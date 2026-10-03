#include "ar_runtime.h"
#include "arcore_session.h"
#include "hw_ar_engine_session.h"
#include <atomic>

#ifdef Q_OS_ANDROID
#include <QJniObject>
#endif

static std::atomic<ArSessionBackend *> s_session{nullptr};
static const char *s_name = "none";

ArSessionBackend *ArRuntime::session()
{
    return s_session.load(std::memory_order_acquire);
}

const char *ArRuntime::name()
{
    return s_name;
}

bool ArRuntime::tryInitialize()
{
    if (s_session.load(std::memory_order_acquire))
        return true;
    if (HwArEngineSession::serverReady()) {
        auto *huawei = HwArEngineSession::instance();
        if (huawei->initialize(0)) {
            s_name = "huawei-ar-engine";
            s_session.store(huawei, std::memory_order_release);
            return true;
        }
    }
    if (ArCoreSession::serviceReady()) {
        auto *google = ArCoreSession::instance();
        if (google->initialize(0)) {
            s_name = "google-arcore";
            s_session.store(google, std::memory_order_release);
            return true;
        }
    }
    return false;
}

bool ArRuntime::requestInstall()
{
    if (s_session.load(std::memory_order_acquire))
        return false;
#ifdef Q_OS_ANDROID
    const QString vendor = QJniObject::getStaticObjectField(
        "android/os/Build", "MANUFACTURER", "Ljava/lang/String;").toString().toLower();
    if ((vendor.contains(QStringLiteral("huawei")) || vendor.contains(QStringLiteral("honor")))
        && HwArEngineSession::installServer())
        return true;
#endif
    return ArCoreSession::requestInstall();
}
