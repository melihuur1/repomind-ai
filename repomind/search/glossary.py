"""Turkish software words, with every sense each can have in code, for what a translator gets wrong.

A translator picks one sense. "yenileme anahtarı" came back as "update key" and "sırayla" as
"queue", and then search looks for the wrong thing. This list keeps all the senses, and a
repository decides which are real: only words its code actually uses are ever searched.

It is general vocabulary, not tuned to one repository. Asking the embedding model for the nearest
repository word was measured first, and found the right word for 8 of 40 cases, going by the look of
a word: "veritabanı" gave "verifiable".

Words are without diacritics, as ``fold`` leaves them. Turkish adds suffixes, so stems match as
prefixes, and a stem that changes under a suffix is listed in both forms (istek, isteği).
"""

from __future__ import annotations

from typing import Container

from repomind.search.tokenizer import _WORD, fold, tokenize

_PHRASES = {
    "kullanici adi": "username",
    "acik metin": "plaintext plain",
    "duz metin": "plaintext plain",
    "zaman asimi": "timeout",
    "ortam degisken": "environment env variable",
    "hiz sinir": "rate limit throttle",
    "yabanci anahtar": "foreign key",
    "uc nokta": "endpoint",
    "on yuz": "frontend",
    "arka uc": "backend",
    "ayni anda": "concurrent simultaneous",
    "en fazla": "max maximum",
    "en az": "min minimum",
    "salt okunur": "readonly",
    "geri al": "rollback undo revert",
    "suresi dol": "expire expired ttl",
    "ne kadar sure": "ttl duration expire",
    "ne siklikla": "interval schedule every",
    "durum kod": "status",
    "cop kutusu": "trash",
    "veri tabani": "database db",
}

# Too short or too common to match as prefixes: "tarafından" is not "tara", and "turkce" is not "tur".
_WHOLE_WORDS = {
    "not": "note",
    "ad": "name",
    "tur": "type",
    "tip": "type",
    "bos": "empty blank null",
    "ret": "reject deny",
}

_STEMS = {
    # signing in and permissions
    "giris": "login signin sign entry",
    "cikis": "logout signout",
    "oturum": "session login",
    "kullanici": "user",
    "sifrele": "encrypt hash",
    "sifre": "password",
    "parola": "password",
    "yetki": "permission role authorization authorize access",
    "izin": "permission allow grant",
    "izni": "permission allow grant",
    "rol": "role",
    "erisim": "access",
    "anahtar": "key token",
    "jeton": "token",
    "kimlik": "identity authentication auth credential id",
    "kimlig": "identity authentication auth credential id",
    "dogrula": "verify validate validation authenticate check confirm",
    "yenile": "refresh renew reload rotate",
    "iptal": "cancel revoke",
    "guvenlik": "security guard",
    "kayit": "register registration signup record log",
    "kaydi": "register registration signup record log",
    "engel": "block deny prevent reject forbid",
    "reddet": "reject deny refuse",
    "kilit": "lock",
    "kilid": "lock",
    "yasak": "forbidden forbid ban",
    # limits and time
    "kisit": "constraint restriction limit check",
    "sinir": "limit max min bound throttle",
    "sure": "duration timeout ttl expiry expire time",
    "gecerli": "valid active",
    "dolan": "expire expired",
    "doldu": "expire expired",
    "zaman": "time timeout timestamp schedule",
    "gecikme": "delay latency",
    "bekle": "wait await pending",
    "tekrar": "retry repeat",
    "yeniden": "retry reconnect again",
    "deneme": "retry try attempt",
    "siklik": "interval frequency",
    "kural": "rule validation constraint",
    "bicim": "format pattern regex",
    "uzunluk": "length size",
    "uzunlug": "length size",
    "boyut": "size",
    "zorunlu": "required notnull mandatory",
    "eksik": "missing",
    # errors and HTTP
    "hata": "error exception failure",
    "basarisiz": "fail failure failed",
    "durum": "status state",
    "yanit": "response reply",
    "cevap": "response reply",
    "cevab": "response reply",
    "istek": "request",
    "isteg": "request",
    "sorgu": "query",
    "baslik": "header title",
    "baslig": "header title",
    "govde": "body",
    "cerez": "cookie",
    "adres": "address url",
    "baglanti": "connection link connect",
    "baglan": "connect connection",
    "sunucu": "server",
    "istemci": "client",
    "yonlendir": "redirect route",
    # data
    "veritaban": "database db",
    "tablo": "table",
    "sutun": "column",
    "satir": "row line",
    "indeks": "index",
    "dizin": "index directory",
    "benzersiz": "unique",
    "tekil": "unique",
    "kopya": "duplicate copy",
    "yabanci": "foreign",
    "iliski": "relation foreign",
    "silin": "delete remove cascade drop",
    "silme": "delete remove cascade drop",
    "siler": "delete remove cascade drop",
    "silen": "delete remove cascade drop",
    "ekle": "add insert create",
    "olustur": "create",
    "guncelle": "update modify",
    "degistir": "change update modify",
    "duzenle": "edit update",
    "okun": "read",
    "okuma": "read",
    "yazma": "write",
    "sakla": "store save persist",
    "kaydet": "save persist store",
    "depola": "store storage",
    "depo": "warehouse repository store storage inventory",
    "alan": "field",
    "deger": "value",
    "tanim": "definition define declared",
    "bilinmeyen": "unknown",
    "turu": "type",
    "tipi": "type",
    "sayac": "counter",
    "surum": "version",
    "gecmis": "history",
    "islem": "transaction operation process",
    "isim": "name",
    "ismi": "name",
    # common in applications
    "stok": "stock inventory",
    "envanter": "inventory",
    "miktar": "quantity amount",
    "adet": "quantity count",
    "sayi": "count number",
    "rezervasyon": "reservation",
    "onay": "confirm approve",
    "siparis": "order",
    "sira": "order sequence queue",
    "kuyruk": "queue",
    "kuyrug": "queue",
    "eszamanli": "concurrent simultaneous",
    "yaris": "race",
    "tutarli": "consistent consistency",
    "dosya": "file",
    "klasor": "folder directory",
    "notlar": "note",
    "notu": "note",
    "notun": "note",
    "paylas": "share sharing",
    "davet": "invite invitation",
    "uye": "member",
    "sahip": "owner",
    "sahib": "owner",
    "izleyici": "viewer",
    "goruntule": "view",
    "duzenleyici": "editor",
    "yonetici": "admin manager",
    "mesaj": "message",
    "bildirim": "notification notify",
    "uyari": "warning alert warn",
    "cevrimdisi": "offline",
    "cevrimici": "online",
    "arsiv": "archive",
    "arama": "search",
    "filtre": "filter",
    "sirala": "sort order",
    "sayfa": "page pagination",
    "metin": "text",
    "metni": "text",
    # configuration and running
    "baslat": "start init bootstrap",
    "durdur": "stop shutdown",
    "kapat": "close shutdown disconnect",
    "calistir": "run start execute",
    "yapilandirma": "config configuration settings",
    "ayar": "config setting configuration",
    "ortam": "environment env",
    "degisken": "variable",
    "varsayilan": "default",
    "ornek": "instance example sample",
    "kume": "cluster",
    "dagitik": "distributed",
    "kesinti": "outage down failure",
    "konteyner": "container",
    "bellek": "memory cache",
    "belleg": "memory cache",
    "onbellek": "cache",
    "gunluk": "log logging",
    "denetim": "audit check",
    "olay": "event",
    "dinle": "listen",
    "yayin": "publish broadcast",
    "abone": "subscribe",
    "tetik": "trigger",
    "zamanlayici": "scheduler timer",
    "tarama": "scan poll sweep",
    "taran": "scan poll sweep",
    "temizle": "cleanup clean purge",
    "eposta": "email mail",
    "posta": "email mail",
}
_LONGEST_FIRST = sorted(_STEMS, key=len, reverse=True)


def repository_terms(question: str, vocabulary: Container[str]) -> list[str]:
    """Every sense of the question's words that this repository's code actually uses."""
    words = [fold(word) for word in _WORD.findall(question)]
    joined = " ".join(words)
    senses = [english for phrase, english in _PHRASES.items() if phrase in joined]
    for word in words:
        if word in _WHOLE_WORDS:
            senses.append(_WHOLE_WORDS[word])
        elif stem := next((stem for stem in _LONGEST_FIRST if word.startswith(stem)), None):
            senses.append(_STEMS[stem])
    terms = dict.fromkeys(term for english in senses for term in english.split())
    return [term for term in terms if all(token in vocabulary for token in tokenize(term))]
