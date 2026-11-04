import pytest

from homebot.voice import addressed


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("Ivy, turn on the bedroom light.", "turn on the bedroom light."),
        ("Hey Ivy what time is it", "what time is it"),
        ("Hi Ivy. It was hand blocking.", "It was hand blocking."),
        ("okay, ivy... lights off", "lights off"),
        ("Ivy.", ""),
        # Not talking to Ivy: room talk, a mention mid-sentence, or a near miss.
        ("I return on the bedroom light.", None),
        ("I told Ivy to turn the light on", None),
        ("The light is the light.", None),
        ("Ivyleague results are out", None),
        ("", None),
    ],
)
def test_addressed(text: str, command: str | None) -> None:
    assert addressed(text, "ivy") == command
