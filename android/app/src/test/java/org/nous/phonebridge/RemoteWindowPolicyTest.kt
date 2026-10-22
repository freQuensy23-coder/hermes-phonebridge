package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test

class RemoteWindowPolicyTest {
    private class Node(
        val windowId: Int = 7,
        val owner: String? = "ordinary.app",
        val password: Boolean = false,
        val children: List<Node?> = emptyList(),
        val advertisedChildren: Int = children.size,
        val bounds: RemoteWindowBounds? = null
    )

    private val adapter = object : RemoteWindowNodeAdapter<Node> {
        override fun windowId(node: Node) = node.windowId
        override fun owner(node: Node) = node.owner
        override fun bounds(node: Node) = node.bounds
        override fun isPassword(node: Node) = node.password
        override fun childCount(node: Node) = node.advertisedChildren
        override fun child(node: Node, index: Int) = node.children.getOrNull(index)
    }
    private val bounds = RemoteWindowBounds(0, 40, 1000, 1800)
    private val root = Node(children = listOf(Node()))
    private fun window(
        id: Int = 7,
        node: Node? = root,
        active: Boolean = true,
        focused: Boolean = true,
        kind: RemoteWindowKind = RemoteWindowKind.APPLICATION,
        rectangle: RemoteWindowBounds = bounds
    ) = RemoteWindowSnapshot(id, kind, active, focused, rectangle, node)
    private fun resolve(windows: List<RemoteWindowSnapshot<Node>>, expected: String? = "ordinary.app", activeRoot: Node = root, geometry: RemoteWindowGeometry? = null, navigationOwner: String? = null) =
        RemoteWindowPolicy.resolve(activeRoot, expected, windows, adapter, { it == "protected.app" || it == "com.android.systemui" }, geometry, navigationOwner)
    private val displayBounds = RemoteWindowBounds(0, 0, 1080, 2400)
    private val statusBounds = RemoteWindowBounds(0, 0, 1080, 63)
    private val navigationBounds = RemoteWindowBounds(0, 2274, 1080, 2400)
    private fun geometry(statusHeight: Int = 63) = RemoteWindowGeometry.fromInsets(
        displayBounds, RemoteWindowInsets(top = statusHeight), RemoteWindowInsets(bottom = 126)
    )!!
    private fun bar(id: Int, rectangle: RemoteWindowBounds) = window(
        id, Node(id, "com.android.systemui", bounds = rectangle), false, false, RemoteWindowKind.SYSTEM, rectangle
    )
    private fun fullGraph() = listOf(window(rectangle = displayBounds), bar(8, statusBounds), bar(9, navigationBounds))
    private fun invalidGesture(block: () -> Unit) {
        try { block(); fail("Expected excluded gesture coordinate refusal") } catch (e: BridgeFailure) {
            assertEquals("VALIDATION_ERROR", e.code)
        }
    }
    private fun refused(block: () -> Unit) {
        try { block(); fail("Expected fail-closed refusal") } catch (e: BridgeFailure) {
            assertTrue(e.code == "UNSUPPORTED" || e.code == "AMBIGUOUS_STATE" || e.code == "FOREGROUND_REQUIRED")
        }
    }

    @Test fun ordinaryMatchingGraphResolvesTheActualSelectedRootAndNativeBounds() {
        val target = resolve(listOf(window()))
        assertSame(root, target.root)
        assertEquals(7, target.windowId)
        assertEquals(bounds, target.bounds)
        assertEquals("ordinary.app", target.owner)
    }
    @Test fun matchingGraphAllowsNoExpectedOwnerForTreeAndGlobalOperations() {
        assertSame(root, resolve(listOf(window()), expected = null).root)
    }
    @Test fun knownDisjointNonfocusedSystemWindowIsNotAnOverlay() {
        val system = window(8, null, false, false, RemoteWindowKind.SYSTEM, RemoteWindowBounds(0, 0, 1000, 40))
        assertSame(root, resolve(listOf(window(), system)).root)
    }
    @Test fun matchingDisjointSameOwnerApplicationIsCompletelyInspected() {
        val other = window(8, Node(8), false, false, rectangle = RemoteWindowBounds(1000, 40, 1400, 1800))
        assertSame(root, resolve(listOf(window(), other)).root)
    }
    @Test fun nativeBoundsContainOnlyNonemptyInWindowGestureBounds() {
        assertTrue(bounds.contains(RemoteWindowBounds(10, 50, 990, 1790)))
        assertTrue(bounds.contains(bounds))
        assertFalse(bounds.contains(RemoteWindowBounds(-1, 50, 990, 1790)))
        assertFalse(bounds.contains(RemoteWindowBounds(10, 50, 1001, 1790)))
        assertFalse(bounds.contains(RemoteWindowBounds(10, 50, 990, 1801)))
        assertFalse(bounds.contains(RemoteWindowBounds(10, 39, 990, 1790)))
        assertFalse(bounds.contains(RemoteWindowBounds(10, 50, 10, 1790)))
    }
    @Test fun independentlyProvedNativeBarsAllowAFullApplicationFrameWithoutChangingCaptureBounds() {
        for (height in listOf(63, 128)) {
            val status = RemoteWindowBounds(0, 0, 1080, height)
            val target = resolve(listOf(window(rectangle = displayBounds), bar(8, status), bar(9, navigationBounds)), geometry = geometry(height))
            assertSame(root, target.root)
            assertEquals(7, target.windowId)
            assertEquals(displayBounds, target.bounds)
            assertEquals(RemoteWindowBounds(0, height, 1080, 2274), target.inputBounds)
        }
    }
    @Test fun observedBarGeometryAloneIsNotNativeProof() {
        refused { resolve(fullGraph()) }
        val noBars = RemoteWindowGeometry.fromInsets(displayBounds, RemoteWindowInsets(), RemoteWindowInsets())!!
        assertTrue(noBars.nativeBarBounds.isEmpty())
        refused { resolve(fullGraph(), geometry = noBars) }
        refused { resolve(fullGraph(), geometry = geometry(64)) }
    }
    @Test fun independentlyVerifiedRecentsOwnerAllowsOnlyTheNativeNavigationBarAndPreservesCaptureBounds() {
        val navigation = bar(9, navigationBounds).copy(root = Node(9, "verified.recents", bounds = navigationBounds))
        val target = resolve(listOf(window(rectangle = displayBounds), bar(8, RemoteWindowBounds(0, 0, 1080, 128)), navigation), geometry = geometry(128), navigationOwner = "verified.recents")
        assertSame(root, target.root)
        assertEquals(displayBounds, target.bounds)
        assertEquals(RemoteWindowBounds(0, 128, 1080, 2274), target.inputBounds)
        target.requireGesturePoint(500, 128)
        target.requireGesturePoint(500, 2273)
        invalidGesture { target.requireGesturePoint(500, 127) }
        invalidGesture { target.requireGesturePoint(500, 2274) }
    }
    @Test fun navigationOwnerMustBeIndependentlyProvenAndMatchExactly() {
        val navigation = bar(9, navigationBounds).copy(root = Node(9, "verified.recents", bounds = navigationBounds))
        val graph = listOf(window(rectangle = displayBounds), navigation)
        refused { resolve(graph, geometry = geometry()) }
        refused { resolve(graph, geometry = geometry(), navigationOwner = "other.recents") }
        refused { resolve(graph, geometry = geometry(), navigationOwner = "") }
        refused { resolve(graph, navigationOwner = "verified.recents") }
        val noNavigation = RemoteWindowGeometry.fromInsets(displayBounds, RemoteWindowInsets(top = 63), RemoteWindowInsets())!!
        refused { resolve(graph, geometry = noNavigation, navigationOwner = "verified.recents") }
    }
    @Test fun verifiedNavigationOwnerCannotExemptStatusInteriorNearBarOrFullScreenWindows() {
        for (rectangle in listOf(statusBounds, RemoteWindowBounds(0, 63, 1080, 126), RemoteWindowBounds(0, 2273, 1080, 2400), RemoteWindowBounds(1, 2274, 1080, 2400), displayBounds)) {
            val candidate = bar(9, rectangle).copy(root = Node(9, "verified.recents", bounds = rectangle))
            refused { resolve(listOf(window(rectangle = displayBounds), candidate), geometry = geometry(), navigationOwner = "verified.recents") }
        }
    }
    @Test fun verifiedNavigationOwnerDoesNotExemptApplicationsOtherTypesFocusOrActivity() {
        val navigation = bar(9, navigationBounds).copy(root = Node(9, "verified.recents", bounds = navigationBounds))
        for (candidate in listOf(navigation.copy(kind = RemoteWindowKind.APPLICATION), navigation.copy(kind = RemoteWindowKind.OTHER), navigation.copy(active = true), navigation.copy(focused = true))) {
            refused { resolve(listOf(window(rectangle = displayBounds), candidate), geometry = geometry(), navigationOwner = "verified.recents") }
        }
        val application = Node(owner = "verified.recents")
        refused { resolve(listOf(window(node = application, rectangle = displayBounds), window(9, Node(9, "verified.recents", bounds = navigationBounds), false, false, RemoteWindowKind.APPLICATION, navigationBounds)), expected = "verified.recents", activeRoot = application, geometry = geometry(), navigationOwner = "verified.recents") }
    }
    @Test fun verifiedNavigationBarRequiresExactRootIdentityBoundsAndCompletePasswordGuard() {
        val navigation = bar(9, navigationBounds).copy(root = Node(9, "verified.recents", bounds = navigationBounds))
        for (candidate in listOf(
            navigation.copy(root = null),
            navigation.copy(root = Node(10, "verified.recents", bounds = navigationBounds)),
            navigation.copy(root = Node(9, "verified.recents")),
            navigation.copy(root = Node(9, "verified.recents", bounds = statusBounds)),
            navigation.copy(root = Node(9, null, bounds = navigationBounds)),
            navigation.copy(root = Node(9, "verified.recents", children = listOf(Node(password = true)), bounds = navigationBounds)),
            navigation.copy(root = Node(9, "verified.recents", advertisedChildren = 1, bounds = navigationBounds)),
            navigation.copy(id = -1)
        )) refused { resolve(listOf(window(rectangle = displayBounds), candidate), geometry = geometry(), navigationOwner = "verified.recents") }
    }
    @Test fun navigationProofUsesOnlyNavigationInsetsIncludingRotatedEdgesAndNeverCutouts() {
        val native = RemoteWindowGeometry.fromInsets(displayBounds, RemoteWindowInsets(top = 63), RemoteWindowInsets(right = 126), RemoteWindowInsets(left = 20, top = 128))!!
        val navigation = RemoteWindowBounds(954, 0, 1080, 2400)
        assertEquals(listOf(navigation), native.navigationBarBounds)
        assertTrue(statusBounds in native.nativeBarBounds)
        assertFalse(statusBounds in native.navigationBarBounds)
        val candidate = bar(9, navigation).copy(root = Node(9, "verified.recents", bounds = navigation))
        val target = resolve(listOf(window(rectangle = displayBounds), candidate), geometry = native, navigationOwner = "verified.recents")
        target.requireGesturePoint(20, 128)
        invalidGesture { target.requireGesturePoint(19, 200) }
        invalidGesture { target.requireGesturePoint(954, 200) }
        val cutout = RemoteWindowBounds(0, 0, 20, 2400)
        refused { resolve(listOf(window(rectangle = displayBounds), bar(9, cutout).copy(root = Node(9, "verified.recents", bounds = cutout))), geometry = native, navigationOwner = "verified.recents") }
    }
    @Test fun nativeProofDoesNotExemptWrongOwnersTypesFocusOrActivity() {
        val original = bar(8, statusBounds)
        val invalid = listOf(
            original.copy(root = Node(8, "ordinary.app", bounds = statusBounds)),
            original.copy(root = Node(8, "protected.app", bounds = statusBounds)),
            original.copy(root = Node(8, null, bounds = statusBounds)),
            original.copy(kind = RemoteWindowKind.APPLICATION),
            original.copy(kind = RemoteWindowKind.OTHER),
            original.copy(active = true),
            original.copy(focused = true)
        )
        for (candidate in invalid) refused { resolve(listOf(window(rectangle = displayBounds), candidate), geometry = geometry()) }
    }
    @Test fun provedBarRequiresExactInspectableRootIdentityAndBounds() {
        val original = bar(8, statusBounds)
        val invalid = listOf(
            original.copy(root = null),
            original.copy(root = Node(9, "com.android.systemui", bounds = statusBounds)),
            original.copy(root = Node(8, "com.android.systemui")),
            original.copy(root = Node(8, "com.android.systemui", bounds = navigationBounds)),
            original.copy(id = -1),
            original.copy(bounds = RemoteWindowBounds(0, 0, 1080, 0))
        )
        for (candidate in invalid) refused { resolve(listOf(window(rectangle = displayBounds), candidate), geometry = geometry()) }
    }
    @Test fun interiorAndNearBarSystemOverlaysAreNotNativeBars() {
        for (rectangle in listOf(
            RemoteWindowBounds(0, 63, 1080, 126),
            RemoteWindowBounds(0, 0, 1080, 62),
            RemoteWindowBounds(1, 0, 1080, 63),
            RemoteWindowBounds(0, 0, 1080, 64),
            RemoteWindowBounds(0, 2273, 1080, 2400),
            displayBounds
        )) refused { resolve(listOf(window(rectangle = displayBounds), bar(8, rectangle)), geometry = geometry()) }
    }
    @Test fun nativeProofDoesNotExemptProtectedApplicationsForeignFocusOrPasswordTrees() {
        val protected = Node(owner = "com.android.systemui")
        refused { resolve(listOf(window(node = protected, rectangle = displayBounds)), expected = "com.android.systemui", activeRoot = protected, geometry = geometry()) }
        refused { resolve(fullGraph() + window(10, Node(10, "foreign.app"), false, true), geometry = geometry()) }
        refused { resolve(fullGraph() + window(10, Node(10), false, false), geometry = geometry()) }
        val password = Node(children = listOf(Node(password = true)))
        refused { resolve(listOf(window(node = password, rectangle = displayBounds), bar(8, statusBounds)), activeRoot = password, geometry = geometry()) }
        val passwordBar = bar(8, statusBounds).copy(root = Node(8, "com.android.systemui", password = true, bounds = statusBounds))
        refused { resolve(listOf(window(rectangle = displayBounds), passwordBar), geometry = geometry()) }
        val incompleteBar = bar(8, statusBounds).copy(root = Node(8, "com.android.systemui", advertisedChildren = 1, bounds = statusBounds))
        refused { resolve(listOf(window(rectangle = displayBounds), incompleteBar), geometry = geometry()) }
    }
    @Test fun tapAndSwipeEndpointsExcludeNativeBarCoordinatesWithoutRejectingTheFullRoot() {
        val target = resolve(fullGraph(), geometry = geometry())
        assertTrue(target.bounds.contains(displayBounds))
        assertEquals(displayBounds, target.bounds)
        target.requireGesturePoint(0, 63)
        target.requireGesturePoint(1079, 2273)
        for ((x, y) in listOf(0 to 0, 500 to 62, 500 to 2274, 1079 to 2399, -1 to 100, 1080 to 100)) {
            invalidGesture { target.requireGesturePoint(x, y) }
        }
        for ((start, end) in listOf((500 to 63) to (500 to 62), (500 to 2274) to (500 to 100))) {
            invalidGesture {
                target.requireGesturePoint(start.first, start.second)
                target.requireGesturePoint(end.first, end.second)
            }
        }
    }
    @Test fun nativeInputExclusionsApplyEvenWithoutVisibleBarWindows() {
        val target = resolve(listOf(window(rectangle = displayBounds)), geometry = geometry())
        invalidGesture { target.requireGesturePoint(500, 0) }
        invalidGesture { target.requireGesturePoint(500, 2399) }
        assertEquals(displayBounds, target.bounds)
    }
    @Test fun independentInsetTypesRetainBothBarsOnTheSameEdgeAndCutoutOnlyRestrictsInput() {
        val native = RemoteWindowGeometry.fromInsets(displayBounds, RemoteWindowInsets(top = 63), RemoteWindowInsets(top = 126), RemoteWindowInsets(left = 20, top = 128))!!
        assertEquals(listOf(statusBounds, RemoteWindowBounds(0, 0, 1080, 126)), native.nativeBarBounds)
        assertEquals(RemoteWindowBounds(20, 128, 1080, 2400), native.inputBounds)
        assertFalse(native.nativeBarBounds.contains(RemoteWindowBounds(0, 0, 20, 2400)))
        val target = resolve(listOf(window(rectangle = displayBounds), bar(8, statusBounds)), geometry = native)
        invalidGesture { target.requireGesturePoint(19, 200) }
        target.requireGesturePoint(20, 128)
        refused { resolve(listOf(window(rectangle = displayBounds), bar(8, RemoteWindowBounds(0, 0, 1080, 128))), geometry = native) }
    }
    @Test fun landscapeAndOffsetNativeGeometryUseTheirOwnEdges() {
        val display = RemoteWindowBounds(100, 200, 2500, 1280)
        val native = RemoteWindowGeometry.fromInsets(display, RemoteWindowInsets(top = 63), RemoteWindowInsets(right = 126))!!
        assertEquals(listOf(RemoteWindowBounds(100, 200, 2500, 263), RemoteWindowBounds(2374, 200, 2500, 1280)), native.nativeBarBounds)
        assertEquals(RemoteWindowBounds(100, 263, 2374, 1280), native.inputBounds)
        val target = resolve(listOf(window(rectangle = display), bar(8, native.nativeBarBounds[1])), geometry = native)
        target.requireGesturePoint(2373, 300)
        invalidGesture { target.requireGesturePoint(2374, 300) }
        assertEquals(display, target.bounds)
    }
    @Test fun nativeGeometryMustContainTheSelectedWindowAndLeaveNonemptyInput() {
        refused { resolve(fullGraph(), geometry = RemoteWindowGeometry.fromInsets(bounds, RemoteWindowInsets(top = 63), RemoteWindowInsets(bottom = 126))) }
        assertNull(RemoteWindowGeometry.fromInsets(displayBounds, RemoteWindowInsets(top = -1), RemoteWindowInsets()))
        assertNull(RemoteWindowGeometry.fromInsets(displayBounds, RemoteWindowInsets(top = 2400), RemoteWindowInsets()))
        assertNull(RemoteWindowGeometry.fromInsets(displayBounds, RemoteWindowInsets(left = 600), RemoteWindowInsets(right = 600)))
        assertNull(RemoteWindowGeometry.fromInsets(RemoteWindowBounds(0, 0, 0, 0), RemoteWindowInsets(), RemoteWindowInsets()))
    }
    @Test fun emptyWindowGraphIsRefused() {
        refused { resolve(emptyList()) }
    }
    @Test fun missingSelectedWindowIsRefused() {
        refused { resolve(listOf(window(8, Node(8)))) }
    }
    @Test fun duplicateSelectedWindowIsRefused() {
        refused { resolve(listOf(window(), window())) }
    }
    @Test fun selectedWindowMustHaveAnInspectableRoot() {
        refused { resolve(listOf(window(node = null))) }
    }
    @Test fun selectedWindowMustBeTheSameNodeAsTheActiveRoot() {
        refused { resolve(listOf(window(node = Node()))) }
    }
    @Test fun selectedWindowRootMustReportItsWindowId() {
        refused { resolve(listOf(window(node = Node(8)))) }
    }
    @Test fun selectedWindowRootMustHaveAKnownOwner() {
        val unknown = Node(owner = null)
        refused { resolve(listOf(window(node = unknown)), activeRoot = unknown) }
    }
    @Test fun selectedWindowMustMatchTheExpectedOwner() {
        refused { resolve(listOf(window()), expected = "different.app") }
    }
    @Test fun selectedWindowMustBeActiveAndFocused() {
        refused { resolve(listOf(window(active = false))) }
        refused { resolve(listOf(window(focused = false))) }
    }
    @Test fun missingFocusedGraphIsRefused() {
        refused { resolve(listOf(window(focused = false), window(8, Node(8), false, false))) }
    }
    @Test fun mismatchedFocusedOwnerIsRefused() {
        refused { resolve(listOf(window(), window(8, Node(8, "foreign.app"), false, true))) }
    }
    @Test fun anotherFocusedWindowWithTheSameOwnerIsAlsoRefused() {
        refused { resolve(listOf(window(), window(8, Node(8), false, true))) }
    }
    @Test fun anotherActiveWindowIsRefused() {
        refused { resolve(listOf(window(), window(8, Node(8), true, false))) }
    }
    @Test fun unknownRelevantApplicationRootIsRefused() {
        refused { resolve(listOf(window(), window(8, null, false, false))) }
    }
    @Test fun unknownRelevantApplicationOwnerIsRefused() {
        refused { resolve(listOf(window(), window(8, Node(8, null), false, false))) }
    }
    @Test fun relevantWindowRootIdMismatchIsRefused() {
        refused { resolve(listOf(window(), window(8, Node(9), false, false))) }
    }
    @Test fun protectedSelectedOwnerIsRefused() {
        val protected = Node(owner = "protected.app")
        refused { resolve(listOf(window(node = protected)), expected = "protected.app", activeRoot = protected) }
    }
    @Test fun protectedNonfocusedApplicationIsRefusedEvenWhenDisjoint() {
        refused { resolve(listOf(window(), window(8, Node(8, "protected.app"), false, false, rectangle = RemoteWindowBounds(1000, 40, 1400, 1800)))) }
    }
    @Test fun focusedSystemWindowIsRefused() {
        refused { resolve(listOf(window(), window(8, Node(8), false, true, RemoteWindowKind.SYSTEM))) }
    }
    @Test fun overlappingNonfocusedSystemWindowIsRefused() {
        refused { resolve(listOf(window(), window(8, Node(8), false, false, RemoteWindowKind.SYSTEM))) }
    }
    @Test fun nonApplicationSelectedWindowIsRefused() {
        refused { resolve(listOf(window(kind = RemoteWindowKind.SYSTEM))) }
        refused { resolve(listOf(window(kind = RemoteWindowKind.OTHER))) }
    }
    @Test fun unknownBoundsCannotHideARelevantWindow() {
        refused { resolve(listOf(window(), window(8, null, false, false, RemoteWindowKind.SYSTEM, RemoteWindowBounds(0, 0, 0, 0)))) }
    }
    @Test fun emptySelectedBoundsAreRefused() {
        refused { resolve(listOf(window(rectangle = RemoteWindowBounds(0, 0, 0, 0)))) }
    }
    @Test fun foreignApplicationIsNotAnOwnershipException() {
        refused { resolve(listOf(window(), window(8, Node(8, "foreign.app"), false, false, rectangle = RemoteWindowBounds(1000, 40, 1400, 1800)))) }
    }
    @Test fun sameOwnerOverlayCannotReceiveSelectedWindowGestures() {
        refused { resolve(listOf(window(), window(8, Node(8), false, false))) }
    }
    @Test fun inputMethodAndOtherWindowTypesHaveNoImplicitFocusException() {
        refused { resolve(listOf(window(), window(8, Node(8, "ordinary.ime"), false, true, RemoteWindowKind.OTHER))) }
        refused { resolve(listOf(window(), window(8, Node(8), false, false, RemoteWindowKind.OTHER))) }
        refused { resolve(listOf(window(), window(8, Node(8, "ordinary.ime"), false, true, RemoteWindowKind.INPUT_METHOD))) }
        refused { resolve(listOf(window(), window(8, Node(8, "ordinary.ime"), true, false, RemoteWindowKind.INPUT_METHOD))) }
    }
    @Test fun visibleKeyboardIsToleratedButCannotReceiveGestures() {
        val keyboard = RemoteWindowBounds(0, 1200, 1000, 1800)
        val target = resolve(listOf(window(), window(8, null, false, false, RemoteWindowKind.INPUT_METHOD, keyboard)))
        assertSame(root, target.root)
        target.requireGesturePoint(500, 600)
        invalidGesture { target.requireGesturePoint(500, 1300) }
    }
    @Test fun selectedPasswordSubtreeIsRefusedByProductionTraversal() {
        val password = Node(children = listOf(Node(password = true)))
        refused { resolve(listOf(window(node = password)), activeRoot = password) }
    }
    @Test fun disjointRelevantPasswordWindowIsRefused() {
        refused { resolve(listOf(window(), window(8, Node(8, children = listOf(Node(8, password = true))), false, false, rectangle = RemoteWindowBounds(1000, 40, 1400, 1800)))) }
    }
    @Test fun advertisedNullChildIsRefusedByTheSameTraversalUsedInProduction() {
        val incomplete = Node(advertisedChildren = 1)
        refused { RemoteWindowPolicy.assertSafeWindow(incomplete, adapter) }
        refused { resolve(listOf(window(node = incomplete)), activeRoot = incomplete) }
    }
    @Test fun laterAdvertisedNullChildCannotBeSkipped() {
        refused { RemoteWindowPolicy.assertSafeWindow(Node(children = listOf(Node(), null, Node())), adapter) }
    }
    @Test fun traversalRetainsChildDepthAndTotalNodeBounds() {
        refused { RemoteWindowPolicy.assertSafeWindow(Node(advertisedChildren = RemoteWindowPolicy.MAX_CHILDREN + 1), adapter) }
        var deep = Node()
        repeat(RemoteWindowPolicy.MAX_DEPTH + 1) { deep = Node(children = listOf(deep)) }
        refused { RemoteWindowPolicy.assertSafeWindow(deep, adapter) }
        val wide = List(250) { Node() }
        refused { RemoteWindowPolicy.assertSafeWindow(Node(children = List(9) { Node(children = wide) }), adapter) }
    }
    @Test fun completeTraversalAcceptsOrdinaryNodesAtItsDepthBoundary() {
        var deep = Node()
        repeat(RemoteWindowPolicy.MAX_DEPTH) { deep = Node(children = listOf(deep)) }
        RemoteWindowPolicy.assertSafeWindow(deep, adapter)
        RemoteWindowPolicy.assertSafeWindow(Node(children = List(7) { Node(children = List(250) { Node() }) }), adapter)
    }
    @Test fun incompleteTraversalRejectsChangingAdvertisedChildCount() {
        val changing = object : RemoteWindowNodeAdapter<Node> by adapter {
            var reads = 0
            override fun childCount(node: Node) = if (++reads == 1) 0 else 1
        }
        refused { RemoteWindowPolicy.assertSafeWindow(Node(), changing) }
    }
}
