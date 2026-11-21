plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.canvds.app"
    compileSdk = 34
    defaultConfig {
        applicationId = "com.canvds.app"
        minSdk = 29                       // MediaStore.Downloads (exporting received files) needs Android 10+
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
        ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a") }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildTypes {
        release { isMinifyEnabled = false }
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"                  // the on-device interpreter; the core itself is plain Python (3.8-compatible)
    }
    sourceSets {
        getByName("main") { srcDir(layout.buildDirectory.dir("pysrc")) }
    }
}

// The Python core is shared with the PC program: copy it from the repository root instead of keeping a second copy.
val syncPython by tasks.registering(Sync::class) {
    from("${rootDir}/../zdiag") { into("zdiag"); exclude("**/__pycache__", "**/*.pyc") }
    from("${rootDir}/../zdiag_ui") { into("zdiag_ui"); exclude("**/__pycache__", "**/*.pyc") }
    into(layout.buildDirectory.dir("pysrc"))
}
tasks.named("preBuild") { dependsOn(syncPython) }
