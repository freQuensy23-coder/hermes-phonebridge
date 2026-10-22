plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val releaseNumber = providers.gradleProperty("phonebridge.release").orNull?.toInt()

android {
    namespace = "org.nous.phonebridge"
    compileSdk = 35
    defaultConfig {
        applicationId = "org.nous.phonebridge"
        minSdk = 31
        targetSdk = 35
        versionCode = releaseNumber ?: 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    signingConfigs {
        if (System.getenv("PHONEBRIDGE_KEYSTORE") != null) {
            create("stableRelease") {
                storeFile = file(System.getenv("PHONEBRIDGE_KEYSTORE"))
                storePassword = System.getenv("PHONEBRIDGE_STORE_PASSWORD")
                keyAlias = System.getenv("PHONEBRIDGE_KEY_ALIAS")
                keyPassword = System.getenv("PHONEBRIDGE_KEY_PASSWORD")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            versionNameSuffix = releaseNumber?.let { "+$it" }
            signingConfig = signingConfigs.findByName("stableRelease")
        }
    }
    testOptions { unitTests.isReturnDefaultValues = false }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    androidTestImplementation("androidx.test:core:1.6.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test:rules:1.6.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
}
