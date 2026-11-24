from apphelper.cover.keys import Unlocker, check, key_char, make_verifier

SHIFT = 0xA0


def vks(text):
    """영문 자판에서 text 를 치는 (vk, shift) 목록."""
    out = []
    shifted_digits = ")!@#$%^&*("
    for ch in text:
        if ch.isalpha():
            out.append((ord(ch.upper()), ch.isupper()))
        elif ch.isdigit():
            out.append((ord(ch), False))
        elif ch in shifted_digits:
            out.append((0x30 + shifted_digits.index(ch), True))
        elif ch == "-":
            out.append((0xBD, False))
        else:
            raise ValueError(ch)
    return out


def type_text(u, text):
    hit = False
    for vk, sh in vks(text):
        if sh:
            u.feed(SHIFT, True)
        hit = u.feed(vk, True) or hit
        u.feed(vk, False)
        if sh:
            u.feed(SHIFT, False)
    return hit


def test_key_char():
    assert key_char(0x41, False, False) == "a" and key_char(0x41, True, False) == "A"
    assert key_char(0x41, False, True) == "A" and key_char(0x41, True, True) == "a"
    assert key_char(0x31, False, False) == "1" and key_char(0x31, True, False) == "!"
    assert key_char(0x61, False, False) == "1"  # 숫자 키패드
    assert key_char(0xBD, True, False) == "_" and key_char(0x0D, False, False) is None  # Enter


def test_verifier_is_salted_and_checks():
    v1, v2 = make_verifier("Pass-12"), make_verifier("Pass-12")
    assert v1["hash"] != v2["hash"] and "Pass" not in str(v1)
    assert check(v1, "Pass-12") and not check(v1, "pass-12")


def test_unlock_with_noise_and_backspace():
    u = Unlocker(make_verifier("Pass-12"))
    assert not type_text(u, "hello")
    assert type_text(u, "xxPass-12")              # 앞에 다른 글자가 섞여도 됨
    u = Unlocker(make_verifier("Pass-12"))
    type_text(u, "Pass-1")
    u.feed(0x33, True)                            # 3 잘못 침
    u.feed(0x08, True)                            # Backspace
    assert type_text(u, "2")
    u = Unlocker(make_verifier("Pass-12"))
    assert not type_text(u, "pass-12")            # 대소문자 구분


def test_caps_lock_tracking():
    u = Unlocker(make_verifier("ABcd"), caps=True)
    assert type_text(u, "abcd") is False          # caps 켜진 상태: abcd → ABCD
    u.feed(0x14, True)                            # Caps Lock 끔
    u = Unlocker(make_verifier("ABcd"), caps=True)
    for vk in (0x41, 0x42):
        u.feed(vk, True)
    u.feed(0x14, True)
    hit = False
    for vk in (0x43, 0x44):
        hit = u.feed(vk, True) or hit
    assert hit


def test_typable():
    from apphelper.cover.keys import typable
    assert typable("Pass-12 !") and not typable("비번1234")
    assert make_verifier("abcd")["typable"] and not make_verifier("한글abcd")["typable"]
