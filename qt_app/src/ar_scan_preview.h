#pragma once

#include <QQuickFramebufferObject>
#include <QOpenGLFunctions>
#include <QOpenGLExtraFunctions>
#include <QOpenGLFramebufferObject>
#include <QTimer>
#include <QByteArray>

/**
 * AR 扫描实时预览。
 *
 * 在渲染线程创建 GL_TEXTURE_EXTERNAL_OES 纹理，把纹理 ID 交给
 * ArScanController（→ AREngine setCameraTextureName + resume），
 * 然后用 OES 采样着色器把相机画面画进 FBO 显示。
 */
class ArScanPreview : public QQuickFramebufferObject
{
    Q_OBJECT
    QML_ELEMENT
public:
    ArScanPreview();

protected:
    QQuickFramebufferObject::Renderer *createRenderer() const override;

private:
    // 可见时周期性请求重绘：Qt 渲染循环是 damage 驱动，ScanPage 静止时
    // render() 不再被调用 → AREngine update/抓帧停摆（黑屏+等待跟踪）。
    QTimer m_refreshTimer;
};

class ArScanPreviewRenderer : public QQuickFramebufferObject::Renderer,
                              protected QOpenGLExtraFunctions
{
public:
    ArScanPreviewRenderer();
    ~ArScanPreviewRenderer() override;

    QOpenGLFramebufferObject *createFramebufferObject(const QSize &size) override;
    void render() override;
    void synchronize(QQuickFramebufferObject *item) override;

private:
    void ensureTextureAndProgram();
    void drawQuadWithUvs(const GLfloat *uvs);
    void renderQuad();
    // 从 GPU 相机纹理回读高清帧（分辨率随 setPreviewSize 提升；比 CPU 640×480 清晰）
    QByteArray captureFromTexture();

    GLuint m_oesTex = 0;
    bool m_texReady = false;
    bool m_texApplied = false; // OES 纹理是否已成功交给 AREngine（启动相机）
    int m_renderCounter = 0;   // 诊断：确认渲染线程持续渲染
    float m_uvs[8] = {0.f, 1.f, 1.f, 1.f, 0.f, 0.f, 1.f, 0.f}; // AREngine 变换后的 UV
    bool m_uvValid = false;    // m_uvs 是否有效（transformDisplayUv 成功）
    GLuint m_program = 0;
    GLint m_posAttr = -1;
    GLint m_uvAttr = -1;
    GLint m_texUni = -1;
    bool m_synced = false;
    QOpenGLFramebufferObject *m_captureFbo = nullptr; // 高清抓帧 FBO（GPU 回读）
    int m_lastCapW = 0; // 最近一次 GPU 回读帧宽（供内参缩放）
    int m_lastCapH = 0;
};
