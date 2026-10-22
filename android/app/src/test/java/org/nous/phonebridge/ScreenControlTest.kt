package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test

class ScreenControlTest {
    @Test fun timedEnableLastsExactlyTheChosenMinutes() {
        for (minutes in listOf(10, 15, 60)) {
            val until = ScreenControl.deadline(minutes, 1_000_000L)
            assertEquals(minutes * 60000L, ScreenControl.remaining(until, 1_000_000L))
            assertEquals(1L, ScreenControl.remaining(until, until - 1))
            assertNull(ScreenControl.remaining(until, until))
        }
    }

    @Test fun untilStoppedNeverExpiresAndZeroIsOff() {
        val until = ScreenControl.deadline(null, 5L)
        assertEquals(ScreenControl.UNTIL_STOPPED, ScreenControl.remaining(until, Long.MAX_VALUE - 1))
        assertNull(ScreenControl.remaining(0L, 5L))
    }
}
