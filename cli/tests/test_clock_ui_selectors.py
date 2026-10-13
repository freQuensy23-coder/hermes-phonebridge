import pytest

from phonebridge.cli import operation_args, parser
from phonebridge.errors import BridgeError


@pytest.mark.parametrize("action", ["enable", "disable", "delete"])
def test_current_clock_alarm_selection_preserves_exact_label_and_display_time(action):
    options = parser().parse_args(["alarms", action, "device", "--package", "com.google.android.deskclock", "--label", "Кофе 🌅", "--time", "07:30"])
    assert operation_args(options) == {"package": "com.google.android.deskclock", "label": "Кофе 🌅", "time": "07:30"}


def test_current_clock_display_time_cannot_be_supplied_twice():
    options = parser().parse_args(["alarms", "disable", "device", "--time", "07:30", "--args", '{"time":"07:30"}'])
    with pytest.raises(BridgeError, match="once"):
        operation_args(options)
