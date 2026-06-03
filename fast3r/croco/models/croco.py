# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (C) 2022-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).


# --------------------------------------------------------
# CroCo model during pretraining
# --------------------------------------------------------


import torch
import torch.nn as nn

torch.backends.cuda.matmul.allow_tf32 = True  # for gpu >= Ampere and pytorch >= 1.12
from functools import partial

from fast3r.croco.models.blocks import Block, DecoderBlock, PatchEmbed
from fast3r.croco.models.masking import RandomMask
from fast3r.croco.models.pos_embed import RoPE2D, get_2d_sincos_pos_embed


class CroCoNet(nn.Module):
    """CroCo 预训练网络，基于交叉视图完成任务训练。

    采用 ViT 编码器 + 交叉注意力解码器的架构，输入一对图像，对第一张图像进行随机遮罩后利用第二张图像的上下文完成重建。
    """

    def __init__(
        self,
        img_size=224,  # input image size
        patch_size=16,  # patch_size
        mask_ratio=0.9,  # ratios of masked tokens
        enc_embed_dim=768,  # encoder feature dimension
        enc_depth=12,  # encoder depth
        enc_num_heads=12,  # encoder number of heads in the transformer block
        dec_embed_dim=512,  # decoder feature dimension
        dec_depth=8,  # decoder depth
        dec_num_heads=16,  # decoder number of heads in the transformer block
        mlp_ratio=4,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        norm_im2_in_dec=True,  # whether to apply normalization of the 'memory' = (second image) in the decoder
        pos_embed="cosine",  # positional embedding (either cosine or RoPE100)
        attn_implementation="pytorch_naive",  # implementation of the scaled_dot_product_attention (either "pytorch_naive" or "flash_attention")
    ):
        """初始化 CroCoNet。

        Args:
            img_size: 输入图像尺寸（正方形）。
            patch_size: 分块大小。
            mask_ratio: 编码器随机遮罩的 patch 比例。
            enc_embed_dim: 编码器特征维度。
            enc_depth: 编码器 Transformer 层数。
            enc_num_heads: 编码器多头注意力头数。
            dec_embed_dim: 解码器特征维度。
            dec_depth: 解码器 Transformer 层数。
            dec_num_heads: 解码器多头注意力头数。
            mlp_ratio: MLP 隱层维度与 Embedding 维度的比值。
            norm_layer: 归一化层类型。
            norm_im2_in_dec: 解码器中是否对第二张图像的深度特征施加归一化。
            pos_embed: 位置编码类型，支持 "cosine" 或 "RoPE{freq}"（如 "RoPE100"）。
            attn_implementation: 注意力实现方式，支持 "pytorch_naive" 或 "flash_attention"。
        """
        super(CroCoNet, self).__init__()

        # patch embeddings  (with initialization done as in MAE)
        self._set_patch_embed(img_size, patch_size, enc_embed_dim)

        # mask generations
        self._set_mask_generator(self.patch_embed.num_patches, mask_ratio)

        self.pos_embed = pos_embed
        if pos_embed == "cosine":
            # positional embedding of the encoder
            enc_pos_embed = get_2d_sincos_pos_embed(
                enc_embed_dim, int(self.patch_embed.num_patches**0.5), n_cls_token=0
            )
            self.register_buffer(
                "enc_pos_embed", torch.from_numpy(enc_pos_embed).float()
            )
            # positional embedding of the decoder
            dec_pos_embed = get_2d_sincos_pos_embed(
                dec_embed_dim, int(self.patch_embed.num_patches**0.5), n_cls_token=0
            )
            self.register_buffer(
                "dec_pos_embed", torch.from_numpy(dec_pos_embed).float()
            )
            # pos embedding in each block
            self.rope = None  # nothing for cosine
        elif pos_embed.startswith("RoPE"):  # eg RoPE100
            self.enc_pos_embed = None  # nothing to add in the encoder with RoPE
            self.dec_pos_embed = None  # nothing to add in the decoder with RoPE
            if RoPE2D is None:
                raise ImportError(
                    "Cannot find cuRoPE2D, please install it following the README instructions"
                )
            freq = float(pos_embed[len("RoPE") :])
            self.rope = RoPE2D(freq=freq)
        else:
            raise NotImplementedError("Unknown pos_embed " + pos_embed)

        self.attn_implementation = attn_implementation
        # transformer for the encoder
        self.enc_depth = enc_depth
        self.enc_embed_dim = enc_embed_dim
        self.enc_blocks = nn.ModuleList(
            [
                Block(
                    enc_embed_dim,
                    enc_num_heads,
                    mlp_ratio,
                    qkv_bias=True,
                    norm_layer=norm_layer,
                    rope=self.rope,
                    attn_implementation=attn_implementation
                )
                for i in range(enc_depth)
            ]
        )
        self.enc_norm = norm_layer(enc_embed_dim)

        # masked tokens
        self._set_mask_token(dec_embed_dim)

        # decoder
        self._set_decoder(
            enc_embed_dim,
            dec_embed_dim,
            dec_num_heads,
            dec_depth,
            mlp_ratio,
            norm_layer,
            norm_im2_in_dec,
        )

        # prediction head
        self._set_prediction_head(dec_embed_dim, patch_size)

        # initializer weights
        self.initialize_weights()

    def _set_patch_embed(self, img_size=224, patch_size=16, enc_embed_dim=768):
        """初始化 patch 嵌入层。

        Args:
            img_size: 输入图像尺寸。
            patch_size: 分块大小。
            enc_embed_dim: 编码器嵌入维度。
        """
        self.patch_embed = PatchEmbed(img_size, patch_size, 3, enc_embed_dim)

    def _set_mask_generator(self, num_patches, mask_ratio):
        """初始化随机遮罩生成器。

        Args:
            num_patches: patch 总数。
            mask_ratio: 遮罩比例（0~1）。
        """
        self.mask_generator = RandomMask(num_patches, mask_ratio)

    def _set_mask_token(self, dec_embed_dim):
        """初始化解码器的遮罩 token 参数。

        Args:
            dec_embed_dim: 解码器嵌入维度，决定 mask token 大小。
        """
        self.mask_token = nn.Parameter(torch.zeros(1, 1, dec_embed_dim))

    def _set_decoder(
        self,
        enc_embed_dim,
        dec_embed_dim,
        dec_num_heads,
        dec_depth,
        mlp_ratio,
        norm_layer,
        norm_im2_in_dec,
    ):
        """初始化解码器组件，包括编解码投影层、DecoderBlock 列表和最终归一化层。

        Args:
            enc_embed_dim: 编码器嵌入维度，用于 decoder_embed 输入维度。
            dec_embed_dim: 解码器嵌入维度。
            dec_num_heads: 解码器注意力头数。
            dec_depth: 解码器层数。
            mlp_ratio: MLP 隐层维度比例。
            norm_layer: 归一化层类型。
            norm_im2_in_dec: 是否对第二张图像特征施加归一化。
        """
        self.dec_depth = dec_depth
        self.dec_embed_dim = dec_embed_dim
        # transfer from encoder to decoder
        self.decoder_embed = nn.Linear(enc_embed_dim, dec_embed_dim, bias=True)
        # transformer for the decoder
        self.dec_blocks = nn.ModuleList(
            [
                DecoderBlock(
                    dec_embed_dim,
                    dec_num_heads,
                    mlp_ratio=mlp_ratio,
                    qkv_bias=True,
                    norm_layer=norm_layer,
                    norm_mem=norm_im2_in_dec,
                    rope=self.rope,
                    attn_implementation=self.attn_implementation
                )
                for i in range(dec_depth)
            ]
        )
        # final norm layer
        self.dec_norm = norm_layer(dec_embed_dim)

    def _set_prediction_head(self, dec_embed_dim, patch_size):
        """初始化像素预测头。

        Args:
            dec_embed_dim: 解码器嵌入维度，作为线性层输入。
            patch_size: 分块大小，输出维度为 patch_size**2 * 3。
        """
        self.prediction_head = nn.Linear(dec_embed_dim, patch_size**2 * 3, bias=True)

    def initialize_weights(self):
        """初始化模型所有可学参数的权重。

        对 patch_embed 调用其自定义初始化方法，使用正态分布初始化 mask_token，
        并对其他线性层和 LayerNorm 层应用标准初始化。
        """
        # patch embed
        self.patch_embed._init_weights()
        # mask tokens
        if self.mask_token is not None:
            torch.nn.init.normal_(self.mask_token, std=0.02)
        # linears and layer norms
        self.apply(self._init_weights)

    def _init_weights(self, m):
        """对单个子模块施加标准权重初始化。

        线性层使用 Xavier Uniform 初始化权重，偏置置零；
        LayerNorm 层偏置置零、权重置一。

        Args:
            m: 待初始化的子模块（通常由 self.apply() 递归调用）。
        """
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def _encode_image(self, image, do_mask=False, return_all_blocks=False):
        """
        image has B x 3 x img_size x img_size
        do_mask: whether to perform masking or not
        return_all_blocks: if True, return the features at the end of every block
                           instead of just the features from the last block (eg for some prediction heads)
        """
        # embed the image into patches  (x has size B x Npatches x C)
        # and get position if each return patch (pos has size B x Npatches x 2)
        x, pos = self.patch_embed(image)
        # add positional embedding without cls token
        if self.enc_pos_embed is not None:
            x = x + self.enc_pos_embed[None, ...]
        # apply masking
        B, N, C = x.size()
        if do_mask:
            masks = self.mask_generator(x)
            x = x[~masks].view(B, -1, C)
            posvis = pos[~masks].view(B, -1, 2)
        else:
            B, N, C = x.size()
            masks = torch.zeros((B, N), dtype=bool)
            posvis = pos
        # now apply the transformer encoder and normalization
        if return_all_blocks:
            out = []
            for blk in self.enc_blocks:
                x = blk(x, posvis)
                out.append(x)
            out[-1] = self.enc_norm(out[-1])
            return out, pos, masks
        else:
            for blk in self.enc_blocks:
                x = blk(x, posvis)
            x = self.enc_norm(x)
            return x, pos, masks

    def _decoder(self, feat1, pos1, masks1, feat2, pos2, return_all_blocks=False):
        """
        return_all_blocks: if True, return the features at the end of every block
                           instead of just the features from the last block (eg for some prediction heads)

        masks1 can be None => assume image1 fully visible
        """
        # encoder to decoder layer
        visf1 = self.decoder_embed(feat1)
        f2 = self.decoder_embed(feat2)
        # append masked tokens to the sequence
        B, Nenc, C = visf1.size()
        if masks1 is None:  # downstreams
            f1_ = visf1
        else:  # pretraining
            Ntotal = masks1.size(1)
            f1_ = self.mask_token.repeat(B, Ntotal, 1).to(dtype=visf1.dtype)
            f1_[~masks1] = visf1.view(B * Nenc, C)
        # add positional embedding
        if self.dec_pos_embed is not None:
            f1_ = f1_ + self.dec_pos_embed
            f2 = f2 + self.dec_pos_embed
        # apply Transformer blocks
        out = f1_
        out2 = f2
        if return_all_blocks:
            _out, out = out, []
            for blk in self.dec_blocks:
                _out, out2 = blk(_out, out2, pos1, pos2)
                out.append(_out)
            out[-1] = self.dec_norm(out[-1])
        else:
            for blk in self.dec_blocks:
                out, out2 = blk(out, out2, pos1, pos2)
            out = self.dec_norm(out)
        return out

    def patchify(self, imgs):
        """
        imgs: (B, 3, H, W)
        x: (B, L, patch_size**2 *3)
        """
        p = self.patch_embed.patch_size[0]
        assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0

        h = w = imgs.shape[2] // p
        x = imgs.reshape(shape=(imgs.shape[0], 3, h, p, w, p))
        x = torch.einsum("nchpwq->nhwpqc", x)
        x = x.reshape(shape=(imgs.shape[0], h * w, p**2 * 3))

        return x

    def unpatchify(self, x, channels=3):
        """
        x: (N, L, patch_size**2 *channels)
        imgs: (N, 3, H, W)
        """
        patch_size = self.patch_embed.patch_size[0]
        h = w = int(x.shape[1] ** 0.5)
        assert h * w == x.shape[1]
        x = x.reshape(shape=(x.shape[0], h, w, patch_size, patch_size, channels))
        x = torch.einsum("nhwpqc->nchpwq", x)
        imgs = x.reshape(shape=(x.shape[0], channels, h * patch_size, h * patch_size))
        return imgs

    def forward(self, img1, img2):
        """
        img1: tensor of size B x 3 x img_size x img_size
        img2: tensor of size B x 3 x img_size x img_size

        out will be    B x N x (3*patch_size*patch_size)
        masks are also returned as B x N just in case
        """
        # encoder of the masked first image
        feat1, pos1, mask1 = self._encode_image(img1, do_mask=True)
        # encoder of the second image
        feat2, pos2, _ = self._encode_image(img2, do_mask=False)
        # decoder
        decfeat = self._decoder(feat1, pos1, mask1, feat2, pos2)
        # prediction head
        out = self.prediction_head(decfeat)
        # get target
        target = self.patchify(img1)
        return out, mask1, target
