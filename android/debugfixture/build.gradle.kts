plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "org.nous.phonebridge.debugfixture"
    compileSdk = 35
    defaultConfig {
        applicationId = "org.nous.phonebridge.debugfixture"
        minSdk = 31
        targetSdk = 35
        versionCode = 1
        versionName = "1"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

androidComponents {
    beforeVariants(selector().withBuildType("release")) { it.enable = false }
}
