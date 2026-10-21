package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test

class RemoteWindowPolicyTest {
    private class Node(
        val windowId: Int = 7,
        val owner: String? = "ordinary.app",
        val password: Boolean = false,
        val children: List<Node?> = emptyList(),
        val advertisedChildren: Int = children.size
    )

    private val adapter = object : RemoteWindowNodeAdapter<Node> {
        override fun windowId(node: Node) = node.windowId
        override fun owner(node: Node) = node.owner
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
    private fun resolve(windows: List<RemoteWindowSnapshot<Node>>, expected: String? = "ordinary.app", activeRoot: Node = root) =
        RemoteWindowPolicy.resolve(activeRoot, expected, windows, adapter) { it == "protected.app" }
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
        refused { RemoteWindowPolicy.assertSafeWindow(Node(advertisedChildren = 101), adapter) }
        var deep = Node()
        repeat(31) { deep = Node(children = listOf(deep)) }
        refused { RemoteWindowPolicy.assertSafeWindow(deep, adapter) }
        val hundred = List(100) { Node() }
        refused { RemoteWindowPolicy.assertSafeWindow(Node(children = List(4) { Node(children = hundred) }), adapter) }
    }
    @Test fun completeTraversalAcceptsOrdinaryNodesAtItsDepthBoundary() {
        var deep = Node()
        repeat(30) { deep = Node(children = listOf(deep)) }
        RemoteWindowPolicy.assertSafeWindow(deep, adapter)
    }
    @Test fun incompleteTraversalRejectsChangingAdvertisedChildCount() {
        val changing = object : RemoteWindowNodeAdapter<Node> by adapter {
            var reads = 0
            override fun childCount(node: Node) = if (++reads == 1) 0 else 1
        }
        refused { RemoteWindowPolicy.assertSafeWindow(Node(), changing) }
    }
}
