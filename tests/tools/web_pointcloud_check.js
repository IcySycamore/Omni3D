/*
 * 校验 web/index.html 内联的点云解码纯函数（decodePointCloud / fillHeightColors）。
 *
 * 为什么需要它：服务器返回的点云格式有两种（嵌套/扁平 × 带不带 RGB），
 * 解码错位会直接表现为「点云糊成一团」或「颜色全黑」，而这类 bug 在
 * 浏览器里很难肉眼定位。这里用 node 直接跑页面里的真实实现。
 *
 * 用法：node tests/tools/web_pointcloud_check.js
 */
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
const src = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");

const START = "// ==================== 点云解码";
const END = "// ==================== 测量：屏幕空间最近点拾取";
const start = src.indexOf(START);
const end = src.indexOf(END);
if (start < 0 || end < 0 || end <= start) {
  console.error("EXTRACT FAILED: 未能定位点云解码实现块");
  process.exit(1);
}
const { decodePointCloud } = new Function(
  src.slice(start, end) + "\nreturn { decodePointCloud };",
)();

let failed = 0;
function check(label, cond, extra = "") {
  if (!cond) {
    failed++;
    console.error(`FAIL  ${label}${extra ? "  " + extra : ""}`);
  } else {
    console.log(`PASS  ${label}`);
  }
}

// ---- 1) 空/非法输入 ----
check("null → null", decodePointCloud(null) === null);
check("[] → null", decodePointCloud([]) === null);
check("扁平 4 元素 → null", decodePointCloud([1, 2, 3, 4]) === null);

// ---- 2) 嵌套 6 元素（服务器现在的默认格式）----
const nested6 = [
  [1, 2, 3, 255, 0, 0],
  [4, 5, 6, 0, 255, 0],
];
let d = decodePointCloud(nested6);
check("嵌套6：count", d && d.count === 2, String(d && d.count));
check("嵌套6：hasRgb", d && d.hasRgb === true);
check(
  "嵌套6：位置正确",
  d && d.positions[0] === 1 && d.positions[1] === 2 && d.positions[5] === 6,
);
check(
  "嵌套6：颜色还原（÷255）",
  d &&
    d.colors[0] === 1 &&
    d.colors[1] === 0 &&
    d.colors[4] === 1 &&
    d.colors[5] === 0,
);

// ---- 3) 扁平 6 元素 ----
d = decodePointCloud([1, 2, 3, 255, 0, 0, 4, 5, 6, 0, 255, 0]);
check("扁平6：count", d && d.count === 2, String(d && d.count));
check("扁平6：hasRgb", d && d.hasRgb === true);

// ---- 4) 嵌套 3 元素 → 高度着色回退 ----
d = decodePointCloud([
  [0, 0, 0],
  [0, 10, 0],
]);
check("嵌套3：count", d && d.count === 2, String(d && d.count));
check("嵌套3：hasRgb=false", d && d.hasRgb === false);
check(
  "嵌套3：高度着色两端不同（蓝 → 白）",
  d && (d.colors[0] !== d.colors[3] || d.colors[2] !== d.colors[5]),
  d
    ? `low=[${d.colors[0]},${d.colors[1]},${d.colors[2]}] high=[${d.colors[3]},${d.colors[4]},${d.colors[5]}]`
    : "",
);
check(
  "嵌套3：高度着色值域在 0..1",
  d && [...d.colors].every((v) => v >= 0 && v <= 1),
);

// ---- 5) 扁平 3 元素（长度非 6 的倍数）----
d = decodePointCloud([0, 0, 0, 0, 10, 0, 0, 20, 0]);
check("扁平3：count", d && d.count === 3, String(d && d.count));
check("扁平3：hasRgb=false", d && d.hasRgb === false);

// ---- 6) 位置未被破坏 ----
d = decodePointCloud([[1.5, -2.5, 3.5, 10, 20, 30]]);
check(
  "位置原样保留",
  d &&
    d.positions[0] === 1.5 &&
    d.positions[1] === -2.5 &&
    d.positions[2] === 3.5,
);
// Float32Array 会舍入，必须用容差比较
const near = (a, b) => Math.abs(a - b) < 1e-6;
check(
  "颜色原样换算",
  d && near(d.colors[0], 10 / 255) && near(d.colors[2], 30 / 255),
  d ? `got=[${d.colors[0]},${d.colors[1]},${d.colors[2]}]` : "",
);

if (failed) {
  console.error(`\n点云解码校验失败：${failed} 项`);
  process.exit(1);
}
console.log("\n点云解码 OK");
