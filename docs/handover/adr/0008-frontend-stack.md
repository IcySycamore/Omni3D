# ADR-0008：前端技术栈 —— Vite + TypeScript + Vue 3 + three.js

- 状态：Accepted
- 日期：2026-09-17
- 关联：ADR-0006（OpenAPI 生成客户端）、[`../01-industry-research.md`](../01-industry-research.md) §1.4

## 背景

参考实现的 panel 是**单文件 `web/index.html`（约 9.5k 行）+ 原生 ES module**：
无框架、无 TS、无构建步骤；three.js 0.160 走 **importmap 从 unpkg CDN** 拉、exif-js 从 jsdelivr 拉；
SHA-256 手写内联（非安全上下文没有 `crypto.subtle`）。

后果：改一处要点开 200 万字符；没有类型检查；**离线/内网/客户环境白屏**（CDN 挂）；
供应链不受控（CDN 上的包被篡改即是受害者）；无指纹/缓存策略；谈不了 CSP。

而 panel 是**用户唯一摸得到的产品表面**（采集、渲染、测量、编辑、设置、历史），比后端更值钱。

## 决策

- **Vite + TypeScript + Vue 3**（`<script setup>` + Pinia + vue-router）；
- **three.js 作为 npm 依赖**（不再 CDN），构建产物自托管；
- 铁律：**`THREE.*` 对象不得进入 Vue 响应式系统** —— 用 `markRaw` / `shallowRef` /
  非响应式模块级单例持有 scene / renderer / camera，否则渲染循环会被 Proxy 拖死（Vue + three.js 的经典坑）；
- **编辑器核心保持命令式**（canvas 生命周期、交互状态机、工具模型不进框架），
  Vue 只负责外围 UI（设置页、历史、账号/官网表格、工具条、状态栏）；
- **OpenAPI → 生成 TS 客户端**：后端 DTO 与前端类型同源（ADR-0006 的 OpenAPI 就是这份契约）；
- **迁移方式**：先拆模块 + 上类型，**保留现有交互逻辑**，不要求一次重写成组件。

## 代价与后果

- **多一条构建链**（node 工具链进 CI），换来类型、按需打包、离线可用、可指纹缓存；
- **CSP/内联脚本**：手写 SHA-256 那类内联实现要挪进模块（或改用 `crypto.subtle` +
  明确要求安全上下文：HTTPS/localhost；局域网 HTTP 下的登录方案需重新设计——**这是必须解决的实际问题**）；
- **SSR 不做**（纯 SPA）：官网 SEO 会差（若在意，可对官网首页单独做静态预渲染，与 panel 无关）；
- **框架不是银弹**：面板的性能瓶颈在渲染循环与点云调度，不在组件树；别把 three.js 塞进组件里"响应式化"。

## 未决

- 是否用 UI 组件库（Naive UI 等）还是沿用现在的自绘玻璃风 CSS（建议：**沿用现有视觉**，
  只在表格/表单类密集处引轻量组件）；
- 局域网 HTTP 下的登录（无 `crypto.subtle`）方案：内置纯 JS 摘要模块，还是强制 HTTPS/mkcert；
- 是否把官网（portal）与 panel 放进**同一 SPA**（现在是两个独立页面；建议保持分离，登录态不混）。
