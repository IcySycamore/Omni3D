---
license: other
license_name: fair-nc
license_link: LICENSE
tags:
- image-to-3d
- model_hub_mixin
- pytorch_model_hub_mixin
library_name: fast3r
repo_url: https://github.com/facebookresearch/fast3r
---



# ⚡️Fast3R - Towards 3D Reconstruction of 1000+ Images in One Forward Pass


*CVPR 2025*

[![Project Website](https://img.shields.io/badge/Fast3R-Website-4CAF50?logo=googlechrome&logoColor=white)](https://fast3r-3d.github.io/)
[![Paper](https://img.shields.io/badge/arXiv-Paper-b31b1b?logo=arxiv&logoColor=b31b1b)](https://arxiv.org/abs/2501.13928)
[![GitHub Repo](https://img.shields.io/badge/GitHub-Code-FFD700?logo=github)](https://github.com/facebookresearch/fast3r)
[![Gradio Demo](https://img.shields.io/badge/Gradio-Demo-orange?style=flat&logo=Gradio&logoColor=red)](https://fast3r.ngrok.app/)
[![Hugging Face Model](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Model-blue)](https://huggingface.co/jedyang97/Fast3R_ViT_Large_512/)



## Using Fast3R in Your Own Project

To use Fast3R in your own project, you can import the `Fast3R` class from `fast3r.models.fast3r` (follow instructions from the [Fast3R GitHub repo](https://github.com/facebookresearch/fast3r) to install) and use it as a regular PyTorch model.

```python
from fast3r.models.fast3r import Fast3R
from fast3r.models.multiview_dust3r_module import MultiViewDUSt3RLitModule

# Load the model from Hugging Face
model = Fast3R.from_pretrained("jedyang97/Fast3R_ViT_Large_512")
model = model.to("cuda")

# [Optional] Create a lightweight lightning module wrapper for the model.
# This provides functions to estimate camera poses, evaluate 3D reconstruction, etc.
# See fast3r/viz/demo.py for an example.
lit_module = MultiViewDUSt3RLitModule.load_for_inference(model)

# Set model to evaluation mode
model.eval()
lit_module.eval()
```

## Citation

```
@InProceedings{Yang_2025_Fast3R,
    title={Fast3R: Towards 3D Reconstruction of 1000+ Images in One Forward Pass},
    author={Jianing Yang and Alexander Sax and Kevin J. Liang and Mikael Henaff and Hao Tang and Ang Cao and Joyce Chai and Franziska Meier and Matt Feiszli},
    booktitle={Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)},
    month={June},
    year={2025},
}
```

## License

The code and models are licensed under the [FAIR NC Research License](LICENSE).