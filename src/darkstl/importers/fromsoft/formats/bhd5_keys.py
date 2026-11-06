"""The RSA public keys that open the archive headers of Dark Souls III, Sekiro and Elden Ring.

The games carry these keys in their executables; they were published by the modding community
(BinderTool by Atvaark, UXM by TKGP and Nordgaren) and are copied here from Smithbox
(https://github.com/vawser/Smithbox, ``src/Andre/Andre.Formats/Util/ArchiveKeys.cs``, MIT licence,
Copyright (c) 2025 Vawser, Copyright (c) 2018 Katalash, Meowmaritus; credited there to GoogleBen).
Dark Souls II ships its keys as ``*KeyCode.pem`` files beside its archives, and Dark Souls: Prepare
to Die Edition doesn't encrypt its headers, so neither needs one here.

Each key is the base64 of a PKCS #1 ``RSAPublicKey``, by archive name (without ``.bhd``; the sound
archives of Elden Ring are under ``sd/``).
"""

from __future__ import annotations

ARCHIVE_KEYS: dict[str, dict[str, str]] = {
    "Dark Souls III": {
        "Data1": (
            "MIIBCwKCAQEA05hqyboW/qZaJ3GBIABFVt1X1aa0/sKINklvpkTRC+5Ytbxvp18LM1gN6gjTgSJi"
            "PUgdlaMbptVa66MzvilEk60aHyVVEhtFWy+HzUZ3xRQm6r/2qsK38wXndgEU5JIT2jrBXZcZfYDC"
            "kUkjsGVkYqjBNKfp+c5jlnNwbieUihWTSEO+DA8naaCCzZD3e7rKhDQyLCkpdsGmuqBvl02Ou7Qe"
            "ehbPPno78mOYs2XkP6NGqbFFGQwaswyyyXlQ23N15ZaFGRRR0xYjrX4LSe6OJ8Mx/Zkec0o7L28C"
            "gwCTmcD2wO8TEATEAUbbV+1Su9uq2+wQxgnsAp+xzhn9og9hmwIEC35bSQ=="
        ),
        "Data2": (
            "MIIBCwKCAQEAvCZAK9UfPdk5JaTlG7n1r0LSVzIan3h0BSLaMXQHOwO7tTGpvtdXm2ZLY9y8SVmO"
            "xWTQqRq14aVGLTKDyH87hPuKd47Y0E5K5erTqBbXW6AD4El1eir2VJz/pwHt73FVziOlAnao1A5M"
            "sAylZ9B5QJyzHJQG+LxzMzmWScyeXlQLOKudfiIG0qFw/xhRMLNAI+iypkzO5NKblYIySUV5Dx76"
            "49XdsZ5UIwJUhxONsKuGS+MbeTFBmTMehtNj5EwPxGdT4CBPAWdeyPhpoHJHCbgrtnN9akwQmpwd"
            "BBxT/sTD16Adn9B+TxuGDQQALed4S4KvM+fadx27pQz8pP9VLwIEL67iCQ=="
        ),
        "Data3": (
            "MIIBCwKCAQEAqLytWD20TSXPeAA1RGDwPW18nJwe2rBX+0HPtdzFmQc/KmQlWrP+94k6KClK5f7m"
            "0xUHwT8+yFGLxPdRvUPyOhBEnRA6tkObVDSxij5y0Jh4h4ilAO73I8VMcmscS71UKkck4444+eR4"
            "vVd+SPlzIu8VgqLefvEn/sX/pAevDp7w+gD0NgvOe9U6iWEXKwTOPB97X+Y2uB03gSSognmV8h2d"
            "tUFJ4Ryn5jrpWmsuUbdvGp0CWBKHCFruNXnfsG0hlf9LqbVmEzbFl/MhjBmbVjjtelorZsoLPK+O"
            "iPTHW5EcwwnPh1vHFFGM7qRMc0yvHqJnniEWDsSz8Bvg+GxpgQIEC8XNVw=="
        ),
        "Data4": (
            "MIIBCwKCAQEArfUaZWjYAUaZ0q+5znpX55GeyepawCZ5NnsMjIW9CA3vrOgUGRkh6aAU9frlafQ8"
            "1LQMRgAznOnQGE7K3ChfySDpq6b47SKm4bWPqd7Ulh2DTxIgi6QPqm4UUJL2dkLaCnuoya/pGMOO"
            "vhT1LD/0CKo/iKwfBcYf/OAnwSnxMRC3SNRugyvFylCet9DEdL5L8uBEa4sV4U288ZxZSZLg2tB1"
            "0xy5SHAsm1VNP4Eqw5iJbqHEDKZWn2LJP5t5wpEJvV2ACiA4U5fyjQLDzRwtCKzeK7yFkKiZI95J"
            "JhU/3DnVvssjIxkugYZkS9D3k9m+tkNe0VVrd4mBEmqVxg+V9wIEL6Y6tw=="
        ),
        "Data5": (
            "MIIBCwKCAQEAvKTlU3nka4nQesRnYg1NWovCCTLhEBAnjmXwI69lFYfc4lvZsTrQE0Y25PtoP0Zd"
            "dA3nzflJNz1rBwAkqfBRGTeeTCAyoNp/iel3EAkid/pKOt3JEkHxrojRuWYSQ0EQawcBbzCfdLEj"
            "izmREepRKHIUSDWgu0HTmwSFHHeCFbpBA1h99L2XizH5XFTOu0UIcUmBLsK6DYsIj5QGrWaxwwXc"
            "TJN/X+/syJ/TbQK9W/TCGaGiirGM1u2wvZXSZ7uVM3CHwgNhAMiqLvqORygcDeNqxgq+dXDTxka4"
            "3j7iPJWdHs8b25fyaH3kbUxKlDGaEENNNyZQcQrgz8Q76jIE0QIEFUsz9w=="
        ),
        "DLC1": (
            "MIIBCwKCAQEAsCGM9dFwzaIOUIin3DXy7xrmI2otKGLZJQyKi5X3znKhSTywpcFcKoW6hgjeh4fJ"
            "W24jhzwBosG6eAzDINm+K02pHCG8qZ/D/hIbu+ui0ENDKqrVyFhnQtX5/QJkVQtj8M4a0FIfdtE3"
            "wkxaKtP6IXWIy4DesSdGWONVWLfi2eq62A5ts5MFqMoSV3XjTYuCgXqZQ6eOE+NIBQRqpZxLNFSz"
            "bJwWXpAg2kBMkpy5+ywOByjmWzUwjnIFl1T17R8DpTU/93ojx+/q1p+b1o5is5KcoP7QwjOqzjHJ"
            "H8bTytzRbgmRcDMW3ahxgI070d45TMXK2YwRzI6/JbM1P29anQIEFezyYw=="
        ),
        "DLC2": (
            "MIIBCwKCAQEAtCXU9a/GBMVoqtpQox9p0/5sWPaIvDp8avLFnIBhN7vkgTwulZHiu64vZAiUAdVe"
            "FX4F+Qtk+5ivK488Mu2CzAMJcz5RvyMQJtOQXuDDqzIv21Tr5zuusswoErHxxP8TZNxkHm7Ram7O"
            "qtn7LQnMTYxsBgZZ34yJkRtAmZnGoCu5YaUR5euk8lF75idi97ssczUNV212tLzIMa1YOV7sxOb7"
            "+gc0VTIqs3pa+OXLPI/bMfwUc/KNjur5aLDDntQHGx5zuNtc78gMGwlmPqDhgTusKPO4VyKvoL0k"
            "ITYvukoXJATaa1HIWVUjhLm+/uj8r8PNgolerDeS+8FM5Bpe9QIEHwCZLw=="
        ),
    },
    "Sekiro": {
        "Data1": (
            "MIIBCwKCAQEA92l+AWx1aV7mzt+6r00bm/qnc4b6NH3VVr/v4UxMcfzushL8jsn9ZSP1ss95ot/q"
            "uk8dOJsp0+/bvxH+C9DEezzNLSqqAGd2jq2PYosj/6FhYAKjjMlKjNxcVPsKQug0Zby+KYsENirm"
            "EXcmA1fzltrISf6d6LKB1UFHHN9NRkLCm3idE4Pu9852kPHbiL14EqfDCDgwm7kLeQdt3kUbcmdh"
            "u/6dvP42HGxBmAYLNFD3iAe7qLMLMFzmKKHQD2fRQK/431Z3xPK6Jp245AdR0AwUYVvnXq+/97wM"
            "X0C6UKvAZ+b/1ytDNu8vZt++lhJ01SjTc2A4hVPz7g1EEO5/TQIEKkj5Jw=="
        ),
        "Data2": (
            "MIIBDAKCAQEAqhjoThWX8VwsTKTI1kjp0JBloCXhV8i99P1KPTCTDBnmhVQPdu+7UQ5g4//eh0oq"
            "KaOUjet+0SP94QscjIIrhV91OzfIouIWgJJK/ROOP/A3sb5AlzPa6YPcN8ODxR+esyrWhc6rHCt4"
            "qGvXVXrgh6zpZM5h5VCTSaup4qqIWm44EF3+FeYS7faFg14rH0QEosieIIZFZmpI6SCJanlrVd+Z"
            "h13s4XcZfk0JdC2AEjxCQ2lKi3UnWAMOcJc+8uHoMuNNo1PMpYQ6Z8Nzg5Cii7EnwbCDmuJw58tF"
            "BmbOVHZpkY93VIeFmaJXSE7ztTp0qTa05YZUsiU3g9HplkeTUwIFAP/xKZE="
        ),
        "Data3": (
            "MIIBDAKCAQEAx5jlgIvoHQLwSFsAwKFZbNo3fgZ89C7tj4hwiZsQVg8QnNZohXl5S5Ep9pS2biOF"
            "sSkuZMXKmfYErh2CsdFbr7QR7kvPPianXNrkCI4xlfQwJvMmkLm96/JmRIUzTWp0kKJUJZJH/UIr"
            "XNn7fmk8Vmx1bQIi8bumGSl3gxeMhutv/lC9khsYTn0ABTJAbIbwNZ5GPXxzQZuQPXXDY52Gm+Fx"
            "7Yy1LiK/B6isIDJUN0xdgxdaXxGNf5pPocMJjng0Ob3cjhGvdkysll/jYFnRx0La3CGmtLcXMtHh"
            "eEQxzGueGDa/lkklAvvEXtcpKfyFQWcUheQZ8LngAh/UTJHtQwIFAOpVoU8="
        ),
        "Data4": (
            "MIIBCwKCAQEAq8RyArk+eqMAcxLAHUDRYV7yScNKZpKSxGmgJZQ7y6Y8f5wdrNCtbyXfmsdQECSt"
            "IGlkwWjtfm8t/bRZuxxPciAYaFsWo0Ze2BB6uY6ZteNpLJn82qbLTXATf+af3kSrvICfvJwRzbfA"
            "/PRJRkHj2gJ6Tc7g6HK7S/4TiCZirq+c/zLY3gb8A8uIFNI4j0qxTzfoAlS7K6spZjfnhZ6l7pYF"
            "h+glz15wAbppC9Oy/u5vUacozf4vnacbUHD47ds9EZPZDHk3LfJbioHwtUzJfyBqZmIpI33yiwIm"
            "Ppb96zwvQU86TaXKsJrTmSs/48BeDsQwXuaqOg+6noETBx3pgQIEGM2Ohw=="
        ),
        "Data5": (
            "MIIBDAKCAQEAu75/UbXwHdvu/p49TwnY7Ou6DAuZYFAtLUkw/R4nvm0HWVlRsZiBLG3MOG6sPmK2"
            "Zc3JLBU2QK4uKazZ9VrmotM4OpYr03q2tiFnv3NfCvB1UeIJIKe3kVhHNZIbvrwEP9a5UCnrSHD+"
            "u+Fj5MQBr4yrEitwrNVvIC4J0Ez1Ppn3+D8ff8XgQRP9qCVLI3X/wdQDea+B5o8PWaYEL9MKnnL1"
            "Tq4h+4PRYHcQR8/GXBTrc3x9q3cPQRDWHbRYhIfWSP9urtagjcsmcuG+p34fp+KyWOwkil3FJqwH"
            "1KgSTbk9Tb0oBPzqTCJKeE/wgu6hY++lBi5T3ArHZZcsbXzV6wIFAPlRTMc="
        ),
    },
    "Elden Ring": {
        "Data0": (
            "MIIBCwKCAQEA9Rju2whruXDVQZpfylVEPeNxm7XgMHcDyaaRUIpXQE0qEo+6Y36LP0xpFvL0H0kK"
            "xHwpuISsdgrnMHJ/yj4S61MWzhO8y4BQbw/zJehhDSRCecFJmFBz3I2JC5FCjoK+82xd9xM5XXdf"
            "sdBzRiSghuIHL4qk2WZ/0f/nK5VygeWXn/oLeYBLjX1S8wSSASza64JXjt0bP/i6mpV2SLZqKRxo"
            "7x2bIQrR1yHNekSF2jBhZIgcbtMBxjCywn+7p954wjcfjxB5VWaZ4hGbKhi1bhYPccht4XnGhcUT"
            "WO3NmJWslwccjQ4ksutLq3uRjLMM0IeTkQO6Pv8/R7UNFtdCWwIERzH8IQ=="
        ),
        "Data1": (
            "MIIBCwKCAQEAxaBCHQJrtLJiJNdG9nq3deA9sY4YCZ4dbTOHO+v+YgWRMcE6iK6oZIJq+nBMUNBb"
            "GPmbRrEjkkH9M7LAypAFOPKC6wMHzqIMBsUMuYffulBuOqtEBD11CAwfx37rjwJ+/1tnEqtJjYkr"
            "K9yyrIN6Y+jy4ftymQtjk83+L89pvMMmkNeZaPON4O9q5M9PnFoKvK8eY45ZV/Jyk+Pe+xc6+e4h"
            "4cx8ML5U2kMM3VDAJush4z/05hS3/bC4B6K9+7dPwgqZgKx1J7DBtLdHSAgwRPpijPeOjKcAa2BD"
            "aNp9Cfon70oC+ZCB+HkQ7FjJcF7KaHsH5oHvuI7EZAl2XTsLEQIENa/2JQ=="
        ),
        "Data2": (
            "MIIBDAKCAQEA0iDVVQ230RgrkIHJNDgxE7I/2AaH6Li1Eu9mtpfrrfhfoK2e7y4OWU+lj7AGI4GI"
            "gkWpPw8JHaV970Cr6+sTG4Tr5eMQPxrCIH7BJAPCloypxcs2BNfTGXzm6veUfrGzLIDp7wy24lIA"
            "8r9ZwUvpKlN28kxBDGeCbGCkYeSVNuF+R9rN4OAMRYh0r1Q950xc2qSNloNsjpDoSKoYN0T7u5rn"
            "Mn/4mtclnWPVRWU940zr1rymv4Jc3umNf6cT1XqrS1gSaK1JWZfsSeD6Dwk3uvquvfY6YlGRygIl"
            "VEMAvKrDRMHylsLtqqhYkZNXMdy0NXopf1rEHKy9poaHEmJldwIFAP////8="
        ),
        "Data3": (
            "MIIBCwKCAQEAvRRNBnVq3WknCNHrJRelcEA2v/OzKlQkxZw1yKll0Y2Kn6G9ts94SfgZYbdFCnIX"
            "y5NEuyHRKrxXz5vurjhrcuoYAI2ZUhXPXZJdgHywac/i3S/IY0V/eDbqepyJWHpP6I565ySqlol1"
            "p/BScVjbEsVyvZGtWIXLPDbx4EYFKA5B52uK6Gdz4qcyVFtVEhNoMvg+EoWnyLD7EUzuB2Khl46C"
            "uNictyWrLlIHgpKJr1QD8a0ld0PDPHDZn03q6QDvZd23UW2d9J+/HeBt52j08+qoBXPwhndZsmPM"
            "WngQDaik6FM7EVRQetKPi6h5uprVmMAS5wR/jQIVTMpTj/zJdwIEXszeQw=="
        ),
        "DLC": (
            "MIIBCwKCAQEAmYJ/5GJU4boJSvZ81BFOHYTGdBWPHnWYly3yWo01BYjGRnz8NTkzDHUxsbjIgtG5"
            "XqsQfZstZILQ97hgSI5AaAoCGrT8sn0PeXg2i0mKwL21gRjRUdvPDp1Y+7hgrGwuTkjycqqsQ/qI"
            "Lm4NvJHvGRd7xLOJ9rs2zwYhceRVrq9XU2AXbdY4pdCQ3+HuoaFiJ0dW0ly5qdEXjbSv2QEYe36n"
            "WCtsd6hEY9LjbBX8D1fK3D2c6C0gNdHJGH2iEONUN6DMK9t0v2JBnwCOZQ7W+Gt7SpNNrkx8xKEM"
            "8gH9na10g9ne11MiO1FnLm8i4zOxVdPHQBKICkKcGS1o3C2dfwIEXw/f3w=="
        ),
        "sd/sd": (
            "MIIBCwKCAQEAmYJ/5GJU4boJSvZ81BFOHYTGdBWPHnWYly3yWo01BYjGRnz8NTkzDHUxsbjIgtG5"
            "XqsQfZstZILQ97hgSI5AaAoCGrT8sn0PeXg2i0mKwL21gRjRUdvPDp1Y+7hgrGwuTkjycqqsQ/qI"
            "Lm4NvJHvGRd7xLOJ9rs2zwYhceRVrq9XU2AXbdY4pdCQ3+HuoaFiJ0dW0ly5qdEXjbSv2QEYe36n"
            "WCtsd6hEY9LjbBX8D1fK3D2c6C0gNdHJGH2iEONUN6DMK9t0v2JBnwCOZQ7W+Gt7SpNNrkx8xKEM"
            "8gH9na10g9ne11MiO1FnLm8i4zOxVdPHQBKICkKcGS1o3C2dfwIEXw/f3w=="
        ),
        "sd/sd_dlc02": (
            "MIIBCwKCAQEAmYJ/5GJU4boJSvZ81BFOHYTGdBWPHnWYly3yWo01BYjGRnz8NTkzDHUxsbjIgtG5"
            "XqsQfZstZILQ97hgSI5AaAoCGrT8sn0PeXg2i0mKwL21gRjRUdvPDp1Y+7hgrGwuTkjycqqsQ/qI"
            "Lm4NvJHvGRd7xLOJ9rs2zwYhceRVrq9XU2AXbdY4pdCQ3+HuoaFiJ0dW0ly5qdEXjbSv2QEYe36n"
            "WCtsd6hEY9LjbBX8D1fK3D2c6C0gNdHJGH2iEONUN6DMK9t0v2JBnwCOZQ7W+Gt7SpNNrkx8xKEM"
            "8gH9na10g9ne11MiO1FnLm8i4zOxVdPHQBKICkKcGS1o3C2dfwIEXw/f3w=="
        ),
    },}


def key_for(game: str, archive: str) -> str | None:
    """The base64 key of ``archive`` (``Data0``, ``DLC``, ``sd/sd``) of ``game``, if one is known."""
    keys = ARCHIVE_KEYS.get(game) or {}
    want = archive.replace("\\", "/").lower()
    return next((k for name, k in keys.items() if name.lower() == want), None)
