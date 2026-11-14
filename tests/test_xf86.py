from fiptui.xf86 import KEY_ACTIONS


def test_expected_xf86_keys_are_mapped():
    assert KEY_ACTIONS == {
        "XF86AudioPlay": "toggle",
        "XF86AudioPause": "toggle",
        "XF86AudioStop": "stop",
        "XF86AudioNext": "next",
        "XF86AudioPrev": "prev",
        "XF86AudioRaiseVolume": "volume_up",
        "XF86AudioLowerVolume": "volume_down",
        "XF86AudioMute": "mute",
    }
