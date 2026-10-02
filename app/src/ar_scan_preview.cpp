#include "ar_scan_preview.h"
#include "ar_scan_controller.h"
#include "hw_ar_engine_session.h"
#include "ar_bridge_server.h"

#include <QOpenGLFramebufferObject>
#include <QQuickWindow>
#include <QBuffer>

#ifdef Q_OS_ANDROID
#include <android/log.h>
#define PLOG(m) __android_log_print(ANDROID_LOG_INFO, "ArScanPreview", "%s", m)
#else
#include <cstdio>
#define PLOG(m) std::fprintf(stderr, "ArScanPreview: %s\n", m)
#endif

#ifndef GL_TEXTURE_EXTERNAL_OES
#define GL_TEXTURE_EXTERNAL_OES 0x8D65
#endif
// Qt QOpenGLFunctions 头未导出以下常量 → 用标准数值
#ifndef GL_TEXTURE_WIDTH
#define GL_TEXTURE_WIDTH 0x1000
#endif
#ifndef GL_TEXTURE_HEIGHT
#define GL_TEXTURE_HEIGHT 0x1001
#endif

ArScanPreview::ArScanPreview()
{
    setMirrorVertically(true);
    setTextureFollowsItemSize(true);

    // 扫描期间持续驱动渲染：QQuickFramebufferObject 的 render() 只在场景图
    // 重绘时调用，而 Qt 渲染循环是 damage 驱动——ScanPage 静止后无变化就不
    // 再渲染 → AREngine update()/抓帧停摆 → 黑屏 + 永远等待跟踪。
    // 可见时每 33ms 请求一次重绘，让渲染线程持续执行 update/抓帧。
    m_refreshTimer.setInterval(33);
    connect(&m_refreshTimer, &QTimer::timeout, this, [this]() { update(); });
    connect(this, &QQuickItem::visibleChanged, this, [this]() {
        if (isVisible()) {
            m_refreshTimer.start();
        } else {
            m_refreshTimer.stop();
        }
    });
    if (isVisible())
        m_refreshTimer.start();
}

QQuickFramebufferObject::Renderer *ArScanPreview::createRenderer() const
{
    return new ArScanPreviewRenderer();
}

// ------------------------------------------------------------
ArScanPreviewRenderer::ArScanPreviewRenderer()
{
    initializeOpenGLFunctions();
}

ArScanPreviewRenderer::~ArScanPreviewRenderer()
{
    if (m_program)
        glDeleteProgram(m_program);
    if (m_oesTex)
        glDeleteTextures(1, &m_oesTex);
    delete m_captureFbo;
    m_captureFbo = nullptr;
}

QOpenGLFramebufferObject *ArScanPreviewRenderer::createFramebufferObject(const QSize &size)
{
    return new QOpenGLFramebufferObject(size, QOpenGLFramebufferObject::NoAttachment);
}

void ArScanPreviewRenderer::synchronize(QQuickFramebufferObject *item)
{
    Q_UNUSED(item);
    m_synced = true;
}

void ArScanPreviewRenderer::ensureTextureAndProgram()
{
    if (m_texReady)
        return;

    // 1) 创建 OES 外部纹理
    glGenTextures(1, &m_oesTex);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, m_oesTex);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, 0);

    // 2) 编译 OES 采样着色器
    const char *vsrc =
        "attribute vec4 position;\n"
        "attribute vec2 texCoord;\n"
        "varying vec2 vTexCoord;\n"
        "void main() { vTexCoord = texCoord; gl_Position = position; }\n";
    const char *fsrc =
        "#extension GL_OES_EGL_image_external : require\n"
        "precision mediump float;\n"
        "uniform samplerExternalOES uTexture;\n"
        "varying vec2 vTexCoord;\n"
        "void main() { gl_FragColor = texture2D(uTexture, vTexCoord); }\n";

    GLuint vs = glCreateShader(GL_VERTEX_SHADER);
    glShaderSource(vs, 1, &vsrc, nullptr);
    glCompileShader(vs);
    GLuint fs = glCreateShader(GL_FRAGMENT_SHADER);
    glShaderSource(fs, 1, &fsrc, nullptr);
    glCompileShader(fs);

    m_program = glCreateProgram();
    glAttachShader(m_program, vs);
    glAttachShader(m_program, fs);
    glLinkProgram(m_program);
    GLint ok = 0;
    glGetProgramiv(m_program, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[512] = {0};
        glGetProgramInfoLog(m_program, sizeof(log), nullptr, log);
        PLOG("shader link failed");
        PLOG(log);
    }
    glDeleteShader(vs);
    glDeleteShader(fs);

    m_posAttr = glGetAttribLocation(m_program, "position");
    m_uvAttr = glGetAttribLocation(m_program, "texCoord");
    m_texUni = glGetUniformLocation(m_program, "uTexture");

    m_texReady = true;
    // 纹理已创建；applyCameraTexture（setCameraTextureName+resume）在 render()
    // 中等待 AREngine 会话就绪后执行（渲染线程 GL 上下文 current）
    PLOG("OES texture created");
}

// 画全屏四边形（纹理坐标由调用方传入；nullptr 用默认直出 UV）
void ArScanPreviewRenderer::drawQuadWithUvs(const GLfloat *uvs)
{
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    glUseProgram(m_program);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, m_oesTex);
    glUniform1i(m_texUni, 0);

    static const GLfloat s_defaultUvs[8] = {0.0f, 1.0f, 1.0f, 1.0f, 0.0f, 0.0f, 1.0f, 0.0f};
    if (!uvs)
        uvs = s_defaultUvs;

    const GLfloat verts[16] = {
        -1.0f, -1.0f, uvs[0], uvs[1],
         1.0f, -1.0f, uvs[2], uvs[3],
        -1.0f,  1.0f, uvs[4], uvs[5],
         1.0f,  1.0f, uvs[6], uvs[7],
    };
    glVertexAttribPointer(static_cast<GLuint>(m_posAttr), 2, GL_FLOAT, GL_FALSE,
                          4 * sizeof(GLfloat), verts);
    glEnableVertexAttribArray(static_cast<GLuint>(m_posAttr));
    glVertexAttribPointer(static_cast<GLuint>(m_uvAttr), 2, GL_FLOAT, GL_FALSE,
                          4 * sizeof(GLfloat), verts + 2);
    glEnableVertexAttribArray(static_cast<GLuint>(m_uvAttr));
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
}

void ArScanPreviewRenderer::renderQuad()
{
    // 全屏四边形（纹理坐标：默认直出；若 AREngine 已按 display rotation 变换则用之，修正画面旋转）
    static const GLfloat s_defaultUvs[8] = {0.0f, 1.0f, 1.0f, 1.0f, 0.0f, 0.0f, 1.0f, 0.0f};
    const GLfloat *uvs = m_uvValid ? m_uvs : s_defaultUvs;
    drawQuadWithUvs(uvs);
}

// 从 GPU 相机纹理回读高清帧（纹理分辨率随 setPreviewSize 提升；
// CPU acquireCameraImage 固定 640×480 → 用 GPU 回读得到更高清图像）。
// 返回 JPEG；纹理不比 CPU 图像大 / 失败时返回空（调用方回退 CPU 路径）。
QByteArray ArScanPreviewRenderer::captureFromTexture()
{
    if (!m_texReady || !m_texApplied || !m_program)
        return {};
    // 查询 OES 相机纹理实际分辨率
    GLint tw = 0, th = 0;
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, m_oesTex);
    glGetTexLevelParameteriv(GL_TEXTURE_EXTERNAL_OES, 0, GL_TEXTURE_WIDTH, &tw);
    glGetTexLevelParameteriv(GL_TEXTURE_EXTERNAL_OES, 0, GL_TEXTURE_HEIGHT, &th);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, 0);
    if (tw <= 0 || th <= 0)
        return {};
    if (tw * th <= 640 * 480)
        return {}; // 纹理与 CPU 同档 → 用 CPU 路径
    // 确保 FBO 尺寸匹配纹理
    if (!m_captureFbo || m_captureFbo->size() != QSize(tw, th)) {
        delete m_captureFbo;
        m_captureFbo = new QOpenGLFramebufferObject(
            QSize(tw, th), QOpenGLFramebufferObject::NoAttachment);
    }
    if (!m_captureFbo || !m_captureFbo->bind())
        return {};
    glViewport(0, 0, tw, th);
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    // ⚠️ 用默认 UV（传感器方向），与 CPU 帧（acquireCameraImage）方向一致，
    //    保证内参/位姿与帧的坐标系匹配；m_uvs 是显示变换，只用于预览
    drawQuadWithUvs(nullptr);
    QByteArray out;
    {
        QVector<unsigned char> px(tw * th * 3);
        glReadPixels(0, 0, tw, th, GL_RGB, GL_UNSIGNED_BYTE, px.data());
        // GL 原点在左下 → 垂直翻转（配合默认 UV 的 V 翻转，净效果=原始传感器方向）
        QImage img(px.constData(), tw, th, tw * 3, QImage::Format_RGB888);
        img = img.copy();
        img = img.mirrored(false, true);
        QBuffer buf(&out);
        buf.open(QIODevice::WriteOnly);
        img.save(&buf, "JPG", 92);
    }
    QOpenGLFramebufferObject::bindDefault();
    if (!out.isEmpty()) {
        m_lastCapW = tw;
        m_lastCapH = th;
    }
    if (out.isEmpty()) {
        char b[96];
        std::snprintf(b, sizeof(b), "captureFromTexture: readback %dx%d -> empty", tw, th);
        PLOG(b);
    }
    return out;
}

void ArScanPreviewRenderer::render()
{
    ensureTextureAndProgram();

    HwArEngineSession *s = HwArEngineSession::instance();

    // 等待 AREngine 会话创建后再把 OES 纹理交给 AREngine 启动相机。
    // ⚠️ 渲染线程可能先于主线程 initialize 运行 → 失败后每帧重试直到成功。
    // setCameraTextureName/resume 必须在 GL 上下文线程执行（主线程无 eglContext）。
    if (m_texReady && !m_texApplied && s->isInitialized()) {
        if (s->applyCameraTexture(static_cast<unsigned int>(m_oesTex))) {
            m_texApplied = true;
            PLOG("camera texture applied OK (render thread)");
        } else {
            PLOG("applyCameraTexture retry...");
        }
    }

    // AREngine：渲染线程（GL 上下文 current）接管 update / 抓帧 / 位姿推送
    // （主线程无 GL 上下文 → AREngine 报 cannot get eglContext，无法跟踪）
    if (s->glOwned()) {
        const bool uok = s->update();
        if (++m_renderCounter % 60 == 0) {
            const auto f = s->frame();
            char buf[96];
            std::snprintf(buf, sizeof(buf), "render #%d update=%d tracking=%d frames=%d",
                          m_renderCounter, uok ? 1 : 0, f.tracking ? 1 : 0,
                          ArScanController::instance()->frameCount());
            PLOG(buf);
        }
        if (uok) {
            const auto f = s->frame();
            // 按 display rotation 变换相机纹理 UV（修正预览旋转 90°）
            static const float uvsIn[8] = {0.f, 1.f, 1.f, 1.f, 0.f, 0.f, 1.f, 0.f};
            if (s->transformDisplayUv(uvsIn, m_uvs, 8))
                m_uvValid = true;
            if (f.tracking) {
                QVector<float> p;
                for (int i = 0; i < 16; ++i)
                    p.append(f.pose[i]);
                ArBridgeServer::instance()->updatePose(p, true, 1.0f);
            }
        }
        if (ArScanController::instance()->consumeCaptureRequest()) {
            // GPU 纹理回读（高清，分辨率随 setPreviewSize）→ 失败回退 CPU 640×480
            QByteArray jpeg = captureFromTexture();
            const bool gpuUsed = !jpeg.isEmpty();
            if (!gpuUsed)
                jpeg = s->captureJpeg();
            if (!jpeg.isEmpty()) {
                const auto f = s->frame();
                QVector<float> pose16, k9;
                for (int i = 0; i < 16; ++i)
                    pose16.append(f.pose[i]);
                for (int i = 0; i < 9; ++i)
                    k9.append(f.k[i]);
                if (gpuUsed) {
                    // ⚠️ AREngine 内参是「显示方向」（如 1056×1420 竖屏），
                    // GPU 帧是「传感器方向横屏」（如 1440×1080）→ 需旋转+缩放：
                    //   fx_sensor=fy_disp, fy_sensor=fx_disp,
                    //   cx_sensor=cy_disp, cy_sensor=width_disp-cx_disp，
                    //   再按 (tw/imgH, th/imgW) 缩放到实际帧尺寸
                    int iw = 0, ih = 0;
                    s->imageDimensions(&iw, &ih);
                    if (iw > 0 && ih > 0 && m_lastCapW > 0 && m_lastCapH > 0) {
                        const float sx = float(m_lastCapW) / float(ih);
                        const float sy = float(m_lastCapH) / float(iw);
                        k9[0] = f.k[4] * sx;
                        k9[2] = f.k[5] * sx;
                        k9[4] = f.k[0] * sy;
                        k9[5] = (float(iw) - f.k[2]) * sy;
                    }
                }
                ArScanController::instance()->storeCaptureResult(jpeg, pose16, k9, f.tracking);
                // 同时累积华为 SLAM 稀疏点云（世界坐标；与抓帧同帧）
                if (f.tracking) {
                    const QVector<float> pc = s->acquirePointCloud();
                    if (!pc.isEmpty())
                        ArScanController::instance()->storePointCloudFrame(pc);
                }
            }
        }
        // 分辨率变更（设置页改后扫描前）：渲染线程暂停→重配→恢复
        if (s->consumeResizePending()) {
            if (s->applyResizeOnRenderThread()) {
                PLOG("camera resolution applied");
            } else {
                PLOG("applyResize failed");
            }
        }
    }

    glViewport(0, 0, framebufferObject()->width(), framebufferObject()->height());
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    renderQuad();
}
