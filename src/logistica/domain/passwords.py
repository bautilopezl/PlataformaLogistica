"""Hash y verificación de contraseñas con argon2id (librería argon2-cffi).

Nunca se guarda ni se registra la contraseña: solo el hash, que incluye sal y parámetros. Si los parámetros
configurados cambian, `necesita_rehash` lo detecta y el login actualiza el hash del usuario.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class Hasher:
    def __init__(
        self, time_cost: int | None = None, memory_cost_kib: int | None = None, parallelism: int | None = None
    ):
        # SUPUESTO (AGENTS.md §16.6): sin valores medidos se usan los de la librería (perfil RFC 9106).
        kwargs = {
            k: v
            for k, v in (
                ("time_cost", time_cost),
                ("memory_cost", memory_cost_kib),
                ("parallelism", parallelism),
            )
            if v is not None
        }
        self._ph = PasswordHasher(**kwargs)
        # Hash de referencia para equiparar tiempos cuando el email no existe (no revela qué emails existen).
        self._dummy = self._ph.hash("contraseña-inexistente")

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verificar(self, password_hash: str | None, password: str) -> bool:
        try:
            return self._ph.verify(password_hash or self._dummy, password) and password_hash is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def necesita_rehash(self, password_hash: str) -> bool:
        return self._ph.check_needs_rehash(password_hash)
