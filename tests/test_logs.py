import json
import logging

import pytest

from spot2ytm import logs


def record(level, msg, exc_info=None, **extra):
    rec = logging.LogRecord("spot2ytm.jobs", level, __file__, 1, msg, None, exc_info)
    rec.__dict__.update(extra)
    return rec


def test_pino_line_has_standard_fields_and_extras():
    line = json.loads(logs.PinoFormatter().format(record(logging.INFO, "song matched", transferId=3)))
    assert line["level"] == 30 and line["msg"] == "song matched" and line["transferId"] == 3
    assert {"time", "pid", "hostname", "name"} <= set(line) and line["module"] == "jobs"
    assert isinstance(line["time"], int)


def test_pino_error_serializer():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        line = json.loads(logs.PinoFormatter().format(record(logging.ERROR, "failed", sys.exc_info())))
    assert line["level"] == 50
    assert line["err"]["type"] == "ValueError" and line["err"]["message"] == "boom"
    assert "Traceback" in line["err"]["stack"]


@pytest.mark.parametrize("name,number", [("trace", 10), ("debug", 20), ("info", 30), ("warn", 40),
                                         ("error", 50), ("fatal", 60)])
def test_pino_levels(name, number):
    assert logs.pino_level(logs.parse_level(name)) == number
    assert logs.parse_level(str(number)) == logs.parse_level(name)


def test_silent_and_unknown_levels():
    assert logs.parse_level("silent") > logging.CRITICAL
    with pytest.raises(ValueError):
        logs.parse_level("loud")
