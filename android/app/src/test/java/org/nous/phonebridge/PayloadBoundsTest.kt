package org.nous.phonebridge

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PayloadBoundsTest {
    private fun rejected(block: () -> Unit) {
        try { block(); fail("Expected bounds rejection") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
    }
    private fun image() = JSONObject().put("mime", "image/jpeg").put("base64", "A".repeat(327680)).put("width", 1280).put("height", 720).put("package", "fixture").put("screen_bounds", JSONArray(listOf(0, 0, 2560, 1520)))
    @Test fun boundedScreenshotResultAcceptsImageButCommandAndEventBudgetsDoNot() {
        val data = image()
        PayloadBounds.validateResult(Results.ok("shot", data), "screen.screenshot")
        rejected { PayloadBounds.validate(data) }
        rejected { PayloadBounds.validate(data, 15000) }
        rejected { PayloadBounds.validateResult(Results.ok("tree", data), "screen.tree") }
    }
    @Test fun fullScreenTreeFitsTheResultBound() {
        val nodes = JSONArray()
        while (nodes.length() < BridgeAccessibility.TREE_NODES) {
            nodes.put(JSONObject().put("resource_id", "com.samsung.android.app.notes:id/item_title").put("class", "android.widget.TextView")
                .put("text", "Заметка о поездке на дачу в субботу утром, купить хлеб".repeat(4).take(120)).put("description", "")
                .put("clickable", true).put("editable", false).put("scrollable", false).put("focused", false).put("checked", false)
                .put("bounds", JSONArray(listOf(0, 0, 1080, 2400))))
        }
        val data = JSONObject().put("package", "com.samsung.android.app.notes").put("nodes", nodes).put("truncated", false).put("untrusted", true)
        assertTrue(data.toString().toByteArray().size in 32769..BridgeAccessibility.TREE_BYTES + 1000)
        PayloadBounds.validateResult(Results.ok("tree", data), "screen.tree")
        for (index in 0 until 4) data.put("padding$index", "x".repeat(16000))
        rejected { PayloadBounds.validateResult(Results.ok("tree", data), "screen.tree") }
    }
    @Test fun screenshotExceptionIsOnlyForTopLevelImageBase64() {
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("text", "x".repeat(16385))), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("nested", JSONObject().put("base64", "A".repeat(20000)))), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("base64", "A".repeat(327684))), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("width", 1281)), "screen.screenshot") }
        rejected { PayloadBounds.validateResult(Results.ok("shot", image().put("screen_bounds", JSONArray(listOf(0, 0, 0, 1)))), "screen.screenshot") }
    }
}
