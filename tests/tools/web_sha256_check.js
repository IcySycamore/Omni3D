/*
 * 校验 web/index.html 内联的纯 JS SHA-256 实现是否与标准一致。
 *
 * 为什么需要它：网页端登录用 sha256(salt+password) 做挑战-应答，
 * 而页面必须用纯 JS 实现（crypto.subtle 只在安全上下文可用，
 * 手机浏览器用 http://<局域网IP>:50865 访问时不可用）。
 * 手写 SHA-256 极易在「填充边界」写错，且服务端会直接判为密码错误，
 * 因此这里用 node 内置 crypto 做交叉验证。
 *
 * 用法：node tests/tools/web_sha256_check.js
 * 退出码非 0 表示实现不一致。
 */ const fs = require("fs");
const crypto = require("crypto");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");

const start = src.indexOf("const _SHA256_K");
const end = src.indexOf("// ==================== 身份：client_id");
if (start < 0 || end < 0) {
  console.error("EXTRACT FAILED: 未能在 index.html 中定位 SHA-256 实现块");
  process.exit(1);
}

const sha256Hex = new Function(src.slice(start, end) + "\nreturn sha256Hex;")();

// 覆盖填充边界（55/56/57/63/64）与多块（1000）以及非 ASCII
const vectors = [
  "",
  "abc",
  "hello world",
  "a".repeat(54),
  "a".repeat(55),
  "a".repeat(56),
  "a".repeat(57),
  "a".repeat(63),
  "a".repeat(64),
  "a".repeat(65),
  "a".repeat(1000),
  "0123456789abcdef" + "p@ssw0rd",
  "中文密码测试",
  "salt-deadbeef" + "хороший пароль",
];

let failed = 0;
for (const v of vectors) {
  const expected = crypto.createHash("sha256").update(v, "utf8").digest("hex");
  const got = sha256Hex(v);
  if (expected !== got) {
    failed++;
    console.error(
      `MISMATCH len=${Buffer.byteLength(v, "utf8")}\n  got      ${got}\n  expected ${expected}`,
    );
  }
}

// ---- 握手协议固定向量 ----
// **必须与 tests/test_auth.py 的 PROTOCOL_VECTORS 完全一致**。
// 网页端登录算的就是这两步：
//     verifier = sha256(salt + password)
//     proof    = sha256(nonce + verifier)
// 拼接顺序与编码（salt/nonce 按 ASCII、密码按 UTF-8）一旦写错，
// 服务器会一律判为「用户名或密码错误」，非常难查。
const PROTOCOL_VECTORS = [
  {
    salt: "a1b2",
    password: "abcd1234",
    nonce: "n0nce",
    verifier:
      "6accaac343825c6fe00011f5a0e55b510252c8df35a16c47eae1db8830c611fe",
    proof: "7c76e8231e4666a2dc2dae3308b4248553b4a9fb37fdaf2cb8d27f387cdef0d7",
  },
  {
    salt: "salt-deadbeef",
    password: "p@ssw0rd-2026",
    nonce: "nonce-xyz",
    verifier:
      "8854270f53b29e8a68e5c4d9410438673190a680604cc4e39c6596c79cc7e350",
    proof: "9f634ba9ddb269bdf66b7719d834bb5965bcddaf0112f0e1eba7b56da0419cfc",
  },
  {
    salt: "00ff",
    password: "____longer_pw_9",
    nonce: "n1",
    verifier:
      "e72d7857f0f6995d96f35751afd5c8220da905cadb2f7ba025c099be55b4bc04",
    proof: "08d394462887142fb0f3e004c3765f286dd4e9862485b3447bf387a40819aa8d",
  },
];

let protocolFailed = 0;
for (const v of PROTOCOL_VECTORS) {
  const verifier = sha256Hex(v.salt + v.password);
  const proof = sha256Hex(v.nonce + verifier);
  if (verifier !== v.verifier) {
    protocolFailed++;
    console.error(
      `协议向量 verifier 不符 salt=${v.salt}\n  got      ${verifier}\n  expected ${v.verifier}`,
    );
  }
  if (proof !== v.proof) {
    protocolFailed++;
    console.error(
      `协议向量 proof 不符 salt=${v.salt}\n  got      ${proof}\n  expected ${v.proof}`,
    );
  }
}
if (protocolFailed === 0) {
  console.log(`协议向量 OK (${PROTOCOL_VECTORS.length} 组)`);
}
failed += protocolFailed;

if (failed) {
  console.error(`SHA256 校验失败：${failed}/${vectors.length}`);
  process.exit(1);
}
console.log(`SHA256 OK (${vectors.length} vectors)`);
