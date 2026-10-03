#pragma once

#include "arsession_backend.h"

class ArRuntime
{
public:
    static ArSessionBackend *session();
    static const char *name();
    static bool tryInitialize();
    static bool requestInstall();
};
