"""Gouverneur de débit basé sur le poids Binance (§2.4 CDC).

Fenêtre glissante de 60 secondes : chaque requête consomme un poids ; avant
d'émettre une requête, on attend que le budget configuré
(`rate_limiter.budget_per_minute`, toujours strictement inférieur à
`MAX_WEIGHT_PER_MINUTE`) ne soit pas dépassé. Le suivi interne se recale sur
l'en-tête faisant autorité `X-MBX-USED-WEIGHT-1M` renvoyé par Binance.

Horloge et fonction d'attente injectables (`time_func`, `sleep_func`) pour
permettre des tests déterministes, sans attente réelle.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0


class BinanceBannedError(RuntimeError):
    """Levée sur une réponse 418 : IP bannie par Binance (2 min à 3 jours).

    Ne jamais retenter automatiquement derrière cette erreur : un retry
    aveugle prolongerait ou aggraverait le bannissement.
    """


class RateLimiter:
    """Gouverneur de poids/minute (fenêtre glissante) + backoff sur 429."""

    def __init__(
        self,
        budget_per_minute: int,
        max_retries: int,
        backoff_base_seconds: float,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self._budget = budget_per_minute
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._time = time_func
        self._sleep = sleep_func
        self._events: deque[tuple[float, int]] = deque()  # (horodatage, poids consommé)
        self._total_consumed = 0  # cumulatif, jamais purgé (contrairement à `_events`)

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def total_consumed(self) -> int:
        """Poids total consommé par CE `RateLimiter` depuis sa création (jamais purgé).

        Ne compte que les appels `acquire()` (les requêtes que ce processus a lui-même
        émises) — pas les ajustements de `sync_from_headers` (qui peuvent refléter le
        trafic d'un autre processus sur la même IP, hors périmètre d'un scan donné).
        """
        return self._total_consumed

    def _purge_expired(self) -> None:
        cutoff = self._time() - WINDOW_SECONDS
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def acquire(self, weight: int) -> None:
        """Bloque jusqu'à ce que `weight` puisse être consommé sans dépasser le budget."""
        while True:
            self._purge_expired()
            usage = sum(w for _, w in self._events)
            if usage + weight <= self._budget:
                self._events.append((self._time(), weight))
                self._total_consumed += weight
                return
            oldest_ts = self._events[0][0]
            wait_seconds = (oldest_ts + WINDOW_SECONDS) - self._time()
            if wait_seconds > 0:
                logger.info(
                    "rate_limiter : budget %d/%d atteint, attente %.1fs",
                    usage, self._budget, wait_seconds,
                )
                self._sleep(wait_seconds)
            else:
                # Garde-fou anti-boucle infinie si l'horloge injectée ne progresse pas.
                self._events.popleft()

    def current_usage(self) -> int:
        """Poids actuellement consommé dans la fenêtre glissante de 60s (transparence/journalisation)."""
        self._purge_expired()
        return sum(w for _, w in self._events)

    def sync_from_headers(self, headers: Mapping[str, str]) -> None:
        """Recale le suivi interne sur l'en-tête faisant autorité `X-MBX-USED-WEIGHT-1M`."""
        raw = headers.get("X-MBX-USED-WEIGHT-1M")
        if raw is None:
            return
        try:
            used = int(raw)
        except ValueError:
            logger.warning("rate_limiter : en-tête X-MBX-USED-WEIGHT-1M illisible : %r", raw)
            return
        self._purge_expired()
        tracked = sum(w for _, w in self._events)
        drift = used - tracked
        if drift > 0:
            # Binance rapporte plus de poids consommé que ce qu'on a suivi
            # (autre appel sur la même IP) : on aligne notre fenêtre dessus.
            self._events.append((self._time(), drift))

    def handle_error_response(
        self, status_code: int, headers: Mapping[str, str], attempt: int
    ) -> None:
        """Gère 429 (backoff puis retour à l'appelant pour retry) et 418 (ban).

        Lève `BinanceBannedError` sur 418 (jamais de retry automatique).
        Lève `RuntimeError` si `max_retries` est dépassé sur des 429 répétés.
        """
        if status_code == 418:
            logger.critical("rate_limiter : IP bannie par Binance (418) — aucun retry automatique.")
            raise BinanceBannedError("IP bannie par Binance (statut 418)")
        if status_code == 429:
            if attempt >= self._max_retries:
                raise RuntimeError(
                    f"rate_limiter : {self._max_retries} tentative(s) épuisée(s) après des 429 répétés"
                )
            retry_after = headers.get("Retry-After")
            wait_seconds: float
            if retry_after is not None:
                try:
                    wait_seconds = float(retry_after)
                except ValueError:
                    wait_seconds = self._backoff_base * (2**attempt)
            else:
                wait_seconds = self._backoff_base * (2**attempt)
            logger.warning(
                "rate_limiter : 429 reçu, backoff %.1fs (tentative %d/%d)",
                wait_seconds, attempt + 1, self._max_retries,
            )
            self._sleep(wait_seconds)
            return
        raise ValueError(f"handle_error_response : statut inattendu {status_code}")
