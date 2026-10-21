package org.nous.phonebridge

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class DebugPayloadBoundsTest {
    private fun rejected(block: () -> Unit) {
        try { block(); fail("Expected bounds rejection") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
    }
    private fun image() = JSONObject().put("mime", "image/jpeg").put("base64", "A".repeat(327680)).put("width", 1280).put("height", 720).put("package", "fixture").put("screen_bounds", JSONArray(listOf(0, 80, 2560, 1520))).put("secure_window_guard", "platform_window_capture")
    @Test fun boundedScreenshotResultAcceptsImageButCommandAndEventBudgetsDoNot() {
        val data = image()
        PayloadBounds.validateResult(Results.ok("shot", data), "screen.screenshot")
        rejected { PayloadBounds.validate(data) }
        rejected { PayloadBounds.validate(data, 15000) }
        rejected { PayloadBounds.validateResult(Results.ok("tree", data), "screen.tree") }
    }
    @Test fun screenshotExceptionIsOnlyForTopLevelImageBase64() {
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("text", "x".repeat(16385))), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("nested", JSONObject().put("base64", "A".repeat(20000)))), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("base64", "A".repeat(327684))), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("width", 1281)), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("screen_bounds", JSONArray(listOf(0, 0, 0, 1)))), "screen.screenshot") }
    }
    @Test fun debugSessionMissingIsPermissionDeniedButWrongTypeIsValidationError() {
        try { DebugArguments.session(Arguments(JSONObject())); fail("Missing session") } catch (e: BridgeFailure) { assertEquals("PERMISSION_REQUIRED", e.code) }
        rejected { DebugArguments.session(Arguments(JSONObject().put("debug_session", 1))) }
        assertEquals("exact-token", DebugArguments.session(Arguments(JSONObject().put("debug_session", "exact-token"))))
    }
}
