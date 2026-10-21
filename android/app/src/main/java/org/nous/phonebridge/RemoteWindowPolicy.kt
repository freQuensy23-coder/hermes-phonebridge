package org.nous.phonebridge

internal interface RemoteWindowNodeAdapter<N> {
    fun windowId(node: N): Int
    fun owner(node: N): String?
    fun bounds(node: N): RemoteWindowBounds?
    fun isPassword(node: N): Boolean
    fun childCount(node: N): Int
    fun child(node: N, index: Int): N?
}

internal enum class RemoteWindowKind { APPLICATION, SYSTEM, OTHER }

internal data class RemoteWindowBounds(val left: Int, val top: Int, val right: Int, val bottom: Int) {
    val nonempty: Boolean get() = left < right && top < bottom
    fun overlaps(other: RemoteWindowBounds) = nonempty && other.nonempty && left < other.right && other.left < right && top < other.bottom && other.top < bottom
    fun contains(other: RemoteWindowBounds) = nonempty && other.nonempty && left <= other.left && top <= other.top && right >= other.right && bottom >= other.bottom
    fun contains(x: Int, y: Int) = nonempty && x >= left && x < right && y >= top && y < bottom
    fun intersect(other: RemoteWindowBounds) = RemoteWindowBounds(maxOf(left, other.left), maxOf(top, other.top), minOf(right, other.right), minOf(bottom, other.bottom))
}

internal data class RemoteWindowInsets(val left: Int = 0, val top: Int = 0, val right: Int = 0, val bottom: Int = 0)

internal class RemoteWindowGeometry private constructor(
    val displayBounds: RemoteWindowBounds,
    val nativeBarBounds: List<RemoteWindowBounds>,
    val navigationBarBounds: List<RemoteWindowBounds>,
    val inputBounds: RemoteWindowBounds
) {
    companion object {
        fun fromInsets(
            displayBounds: RemoteWindowBounds,
            statusBars: RemoteWindowInsets,
            navigationBars: RemoteWindowInsets,
            displayCutout: RemoteWindowInsets = RemoteWindowInsets()
        ): RemoteWindowGeometry? {
            if (!displayBounds.nonempty) return null
            val insets = listOf(statusBars, navigationBars, displayCutout)
            if (insets.any { it.left < 0 || it.top < 0 || it.right < 0 || it.bottom < 0 }) return null
            val left = insets.maxOf { it.left }
            val top = insets.maxOf { it.top }
            val right = insets.maxOf { it.right }
            val bottom = insets.maxOf { it.bottom }
            if (left.toLong() + right >= displayBounds.right.toLong() - displayBounds.left || top.toLong() + bottom >= displayBounds.bottom.toLong() - displayBounds.top) return null
            fun barBounds(bar: RemoteWindowInsets) = buildList {
                if (bar.left > 0) add(RemoteWindowBounds(displayBounds.left, displayBounds.top, displayBounds.left + bar.left, displayBounds.bottom))
                if (bar.top > 0) add(RemoteWindowBounds(displayBounds.left, displayBounds.top, displayBounds.right, displayBounds.top + bar.top))
                if (bar.right > 0) add(RemoteWindowBounds(displayBounds.right - bar.right, displayBounds.top, displayBounds.right, displayBounds.bottom))
                if (bar.bottom > 0) add(RemoteWindowBounds(displayBounds.left, displayBounds.bottom - bar.bottom, displayBounds.right, displayBounds.bottom))
            }
            val navigationBounds = barBounds(navigationBars)
            return RemoteWindowGeometry(displayBounds, (barBounds(statusBars) + navigationBounds).distinct(), navigationBounds, RemoteWindowBounds(displayBounds.left + left, displayBounds.top + top, displayBounds.right - right, displayBounds.bottom - bottom))
        }
    }
}

internal data class RemoteWindowSnapshot<N>(
    val id: Int,
    val kind: RemoteWindowKind,
    val active: Boolean,
    val focused: Boolean,
    val bounds: RemoteWindowBounds,
    val root: N?
)

internal data class RemoteWindowTarget<N>(val root: N, val windowId: Int, val owner: String, val bounds: RemoteWindowBounds, val inputBounds: RemoteWindowBounds = bounds) {
    fun requireGesturePoint(x: Int, y: Int) {
        if (!inputBounds.contains(x, y)) failure("VALIDATION_ERROR", "Gesture coordinates must exclude native system bars and display cutouts")
    }
}

internal object RemoteWindowPolicy {
    fun <N : Any> assertSafeWindow(root: N, adapter: RemoteWindowNodeAdapter<N>) {
        var count = 0
        fun visit(node: N, depth: Int) {
            val children = adapter.childCount(node)
            if (depth > 30 || ++count > 400 || children !in 0..100) failure("UNSUPPORTED", "Window exceeds complete password-guard traversal bound")
            if (adapter.isPassword(node)) failure("UNSUPPORTED", "Password-bearing windows are excluded")
            for (index in 0 until children) {
                val child = adapter.child(node, index) ?: failure("UNSUPPORTED", "Cannot inspect advertised window child")
                visit(child, depth + 1)
            }
            if (adapter.childCount(node) != children) failure("AMBIGUOUS_STATE", "Window changed during password-guard traversal")
            if (adapter.isPassword(node)) failure("UNSUPPORTED", "Password-bearing windows are excluded")
        }
        visit(root, 0)
    }

    fun <N : Any> resolve(
        activeRoot: N,
        expectedOwner: String?,
        windows: List<RemoteWindowSnapshot<N>>,
        adapter: RemoteWindowNodeAdapter<N>,
        protectedOwner: (String) -> Boolean,
        nativeGeometry: RemoteWindowGeometry? = null,
        verifiedNavigationOwner: String? = null
    ): RemoteWindowTarget<N> {
        val selectedId = adapter.windowId(activeRoot)
        if (selectedId < 0 || windows.isEmpty()) failure("UNSUPPORTED", "No inspectable active window graph")
        if (windows.map { it.id }.toSet().size != windows.size) failure("AMBIGUOUS_STATE", "Window graph contains duplicate identities")
        val selected = windows.singleOrNull { it.id == selectedId } ?: failure("UNSUPPORTED", "Selected active window is missing")
        val selectedRoot = selected.root ?: failure("UNSUPPORTED", "Cannot inspect selected window")
        if (selectedRoot != activeRoot || adapter.windowId(selectedRoot) != selectedId) failure("AMBIGUOUS_STATE", "Active root does not match selected window root")
        val owner = adapter.owner(selectedRoot)?.takeIf { it.isNotBlank() } ?: failure("UNSUPPORTED", "Unknown selected window owner")
        if (adapter.owner(activeRoot) != owner) failure("AMBIGUOUS_STATE", "Active root and selected window owners differ")
        if (expectedOwner != null && owner != expectedOwner) failure("FOREGROUND_REQUIRED", "Selected window owner changed")
        if (!selected.active || !selected.focused || selected.kind != RemoteWindowKind.APPLICATION || !selected.bounds.nonempty) failure("UNSUPPORTED", "Selected window must be an active focused application with native bounds")
        val geometry = nativeGeometry?.takeIf { it.displayBounds.contains(selected.bounds) }
        for (window in windows) {
            val relevant = window.active || window.focused || window.kind != RemoteWindowKind.SYSTEM || !window.bounds.nonempty || window.bounds.overlaps(selected.bounds)
            if (!relevant) continue
            val node = window.root ?: failure("UNSUPPORTED", "Cannot inspect relevant window")
            val otherOwner = adapter.owner(node)?.takeIf { it.isNotBlank() } ?: failure("UNSUPPORTED", "Unknown relevant window owner")
            if (window.id < 0 || adapter.windowId(node) != window.id || !window.bounds.nonempty) failure("AMBIGUOUS_STATE", "Relevant root and native window do not correspond")
            val nativeBar = geometry != null && window.kind == RemoteWindowKind.SYSTEM && !window.active && !window.focused && adapter.bounds(node) == window.bounds && (
                (otherOwner == "com.android.systemui" && window.bounds in geometry.nativeBarBounds) ||
                    (otherOwner == verifiedNavigationOwner && window.bounds in geometry.navigationBarBounds)
                )
            if (nativeBar) {
                assertSafeWindow(node, adapter)
                continue
            }
            if (protectedOwner(otherOwner)) failure("UNSUPPORTED", "Protected UI window is present")
            if (window.kind != RemoteWindowKind.APPLICATION) failure("UNSUPPORTED", "System, input method and overlay windows are excluded")
            if ((window.active || window.focused) && window.id != selectedId) failure("AMBIGUOUS_STATE", "Active or focused window differs from selected window")
            if (otherOwner != owner) failure("UNSUPPORTED", "Relevant window has a different owner")
            if (window.id != selectedId && window.bounds.overlaps(selected.bounds)) failure("UNSUPPORTED", "Another window overlaps the selected native window")
            assertSafeWindow(node, adapter)
        }
        return RemoteWindowTarget(selectedRoot, selectedId, owner, selected.bounds, geometry?.inputBounds?.intersect(selected.bounds) ?: selected.bounds)
    }
}
