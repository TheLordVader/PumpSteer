def test_pi_elapsed_time_uses_actual_time_across_dst():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from custom_components.pumpsteer.control import PIController

    tz = ZoneInfo("Europe/Stockholm")
    for a, b in [
        (
            datetime(2026, 3, 29, 1, 59, tzinfo=tz),
            datetime(2026, 3, 29, 3, 0, tzinfo=tz),
        ),
        (
            datetime(2026, 10, 25, 2, 59, tzinfo=tz, fold=0),
            datetime(2026, 10, 25, 2, 0, tzinfo=tz, fold=1),
        ),
    ]:
        c = PIController(emit_log=False)
        c.reset(a)
        assert c.compute(21, 20, 0, 0, b, ki=1).i_term == 1
