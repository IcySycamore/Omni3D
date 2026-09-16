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
 */
const fs = require("fs");
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

if (failed) {
  console.error(`SHA256 校验失败：${failed}/${vectors.length}`);
  process.exit(1);
}
console.log(`SHA256 OK (${vectors.length} vectors)`);
