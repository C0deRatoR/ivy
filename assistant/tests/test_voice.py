import pytest

from homebot.voice import strip_wake_word


@pytest.mark.parametrize(
    ("text", "wake_word", "command"),
    [
        ("Ivy, turn on the bedroom light.", "ivy", "turn on the bedroom light."),
        ("Hey Ivy what time is it", "ivy", "what time is it"),
        ("Ivy.", "ivy", ""),
        ("Hey Jarvis, pause ad blocking", "hey_jarvis", "pause ad blocking"),
        ("hey, jarvis... lights off", "hey_jarvis", "lights off"),
        # Whisper missed or garbled the wake word: the whole transcript is the request.
        ("What time is it?", "hey_jarvis", "What time is it?"),
        ("These are this what time is it?", "hey_jarvis", "These are this what time is it?"),
        # Whole words only.
        ("Check the ivyleague results", "ivy", "Check the ivyleague results"),
        ("", "ivy", ""),
    ],
)
def test_strip_wake_word(text: str, wake_word: str, command: str) -> None:
    assert strip_wake_word(text, wake_word) == command
