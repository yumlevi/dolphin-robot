from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from aiohttp import ClientResponseError, ClientSession

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.dispatcher import dispatcher_send

from ..common.connectivity_status import ConnectivityStatus
from ..common.consts import (
    API_RESPONSE_ALERT,
    API_RESPONSE_DATA,
    API_RESPONSE_UNIT_SERIAL_NUMBER,
    API_TOKEN_FIELDS,
    AUTHENTICATE_USER_URL,
    AWS_STS_TOKEN_URL,
    BEARER_HEADERS_BASE,
    COGNITO_AUTH_FLOW_CUSTOM,
    COGNITO_AUTH_FLOW_REFRESH,
    COGNITO_CHALLENGE_NAME,
    COGNITO_CLIENT_ID,
    COGNITO_CONTENT_TYPE,
    COGNITO_ENDPOINT,
    COGNITO_HEADER_TARGET,
    COGNITO_TARGET_PREFIX,
    DATA_ROBOT_DETAILS,
    ID_TOKEN_REFRESH_WINDOW_SECONDS,
    SIGNAL_API_STATUS,
    SIGNAL_DEVICE_NEW,
)
from ..models.config_data import ConfigData
from ..models.exceptions import LoginError
from .config_manager import ConfigManager

_LOGGER = logging.getLogger(__name__)


def _bearer_headers(id_token: str, extra: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {id_token}",
        **BEARER_HEADERS_BASE,
    }
    if extra:
        headers.update(extra)
    return headers


async def _cognito_call(session: ClientSession, target: str, body: dict) -> dict:
    headers = {
        "Content-Type": COGNITO_CONTENT_TYPE,
        COGNITO_HEADER_TARGET: f"{COGNITO_TARGET_PREFIX}{target}",
    }
    try:
        async with session.post(
            COGNITO_ENDPOINT, headers=headers, data=json.dumps(body)
        ) as response:
            text = await response.text()
            if response.status >= 400:
                _LOGGER.debug(
                    f"Cognito {target} failed, Status: {response.status}, Body: {text}"
                )
                raise LoginError(f"Cognito {target} returned {response.status}")
            return json.loads(text)
    except LoginError:
        raise
    except Exception as ex:
        _LOGGER.debug(f"Cognito {target} request failed, Error: {ex}")
        raise LoginError(f"Cognito {target} request failed: {ex}") from ex


async def cognito_initiate_auth(session: ClientSession, email: str) -> dict:
    response = await _cognito_call(
        session,
        "InitiateAuth",
        {
            "AuthFlow": COGNITO_AUTH_FLOW_CUSTOM,
            "ClientId": COGNITO_CLIENT_ID,
            "AuthParameters": {"USERNAME": email},
            "ClientMetadata": {},
        },
    )
    if response.get("ChallengeName") != COGNITO_CHALLENGE_NAME:
        raise LoginError(
            f"Unexpected Cognito challenge: {response.get('ChallengeName')}"
        )
    return response


async def cognito_respond_otp(
    session: ClientSession, email: str, cognito_session: str, code: str
) -> dict:
    response = await _cognito_call(
        session,
        "RespondToAuthChallenge",
        {
            "ChallengeName": COGNITO_CHALLENGE_NAME,
            "ClientId": COGNITO_CLIENT_ID,
            "Session": cognito_session,
            "ChallengeResponses": {"USERNAME": email, "ANSWER": code},
            "ClientMetadata": {},
        },
    )
    auth = response.get("AuthenticationResult")
    if not auth or "IdToken" not in auth:
        raise LoginError("OTP rejected by Cognito")
    return auth


async def cognito_refresh(session: ClientSession, refresh_token: str) -> dict:
    response = await _cognito_call(
        session,
        "InitiateAuth",
        {
            "AuthFlow": COGNITO_AUTH_FLOW_REFRESH,
            "ClientId": COGNITO_CLIENT_ID,
            "AuthParameters": {"REFRESH_TOKEN": refresh_token},
            "ClientMetadata": {},
        },
    )
    auth = response.get("AuthenticationResult")
    if not auth or "IdToken" not in auth:
        raise LoginError("Refresh token rejected by Cognito")
    return auth


async def fetch_user_profile(session: ClientSession, id_token: str) -> dict:
    headers = _bearer_headers(
        id_token,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        async with session.post(
            AUTHENTICATE_USER_URL, headers=headers, data=""
        ) as response:
            response.raise_for_status()
            payload = await response.json()
    except ClientResponseError as ex:
        raise LoginError(f"authenticate-user failed: HTTP {ex.status}") from ex
    except Exception as ex:
        raise LoginError(f"authenticate-user request failed: {ex}") from ex

    data = payload.get(API_RESPONSE_DATA) or {}
    if not data:
        raise LoginError(
            f"authenticate-user returned empty data, Alert: {payload.get(API_RESPONSE_ALERT)}"
        )
    return data


async def fetch_aws_credentials(session: ClientSession, id_token: str) -> dict:
    headers = _bearer_headers(id_token)
    try:
        async with session.get(AWS_STS_TOKEN_URL, headers=headers) as response:
            response.raise_for_status()
            payload = await response.json()
    except ClientResponseError as ex:
        raise LoginError(f"getToken failed: HTTP {ex.status}") from ex
    except Exception as ex:
        raise LoginError(f"getToken request failed: {ex}") from ex

    data = payload.get(API_RESPONSE_DATA) or {}
    if not data.get("AccessKeyId"):
        raise LoginError(
            f"getToken returned no credentials, Alert: {payload.get(API_RESPONSE_ALERT)}"
        )
    return data


class RestAPI:
    data: dict

    _hass: HomeAssistant | None
    _status: ConnectivityStatus | None
    _session: ClientSession | None
    _config_manager: ConfigManager
    _device_loaded: bool

    def __init__(self, hass: HomeAssistant | None, config_manager: ConfigManager):
        try:
            self._hass = hass
            self.data = {}
            self._config_manager = config_manager
            self._status = None
            self._session = None
            self._device_loaded = False
            self._local_async_dispatcher_send = None

        except Exception as ex:
            exc_type, exc_obj, tb = sys.exc_info()
            line_number = tb.tb_lineno
            _LOGGER.error(
                f"Failed to load MyDolphin Plus API, error: {ex}, line: {line_number}"
            )

    @property
    def is_connected(self):
        return self._session is not None

    @property
    def config_data(self) -> ConfigData:
        return self._config_manager.config_data

    @property
    def status(self) -> str | None:
        return self._status

    @property
    def _is_home_assistant(self):
        return self._hass is not None

    async def initialize(self):
        _LOGGER.info("Initializing MyDolphin API")
        await self._initialize_session()
        await self._login()

    async def terminate(self):
        if self._session is not None:
            await self._session.close()
            self._set_status(ConnectivityStatus.DISCONNECTED, "terminate requested")

    async def _initialize_session(self):
        try:
            if self._is_home_assistant:
                self._session = async_create_clientsession(hass=self._hass)
            else:
                self._session = ClientSession()

        except Exception as ex:
            exc_type, exc_obj, tb = sys.exc_info()
            line_number = tb.tb_lineno
            message = (
                f"Failed to initialize session, Error: {str(ex)}, Line: {line_number}"
            )
            self._set_status(ConnectivityStatus.FAILED, message)

    async def update(self):
        if self._status != ConnectivityStatus.CONNECTED:
            return

        _LOGGER.debug("Connected. Refresh details")

        if not await self._ensure_id_token_valid():
            return

        if not await self._authenticate_user():
            return

        if not self._device_loaded:
            self._device_loaded = True
            self._async_dispatcher_send(
                SIGNAL_DEVICE_NEW, self._config_manager.entry_id
            )

        _LOGGER.debug(f"API Data updated: {self.data}")

    async def _login(self):
        if self._config_manager.refresh_token is None:
            self._set_status(
                ConnectivityStatus.EXPIRED_TOKEN,
                "no refresh token stored — remove and re-add the integration",
            )
            return

        if not await self._ensure_id_token_valid():
            return

        if not await self._authenticate_user():
            return

        self._set_status(
            ConnectivityStatus.TEMPORARY_CONNECTED,
            f"profile loaded for {self._config_manager.serial_number}",
        )

        await self._get_aws_credentials()

    async def _ensure_id_token_valid(self) -> bool:
        expires_at = self._config_manager.id_token_expires_at or 0
        id_token = self._config_manager.id_token
        now = time.time()

        if id_token and (expires_at - now) > ID_TOKEN_REFRESH_WINDOW_SECONDS:
            return True

        refresh_token = self._config_manager.refresh_token
        if not refresh_token:
            self._set_status(
                ConnectivityStatus.EXPIRED_TOKEN,
                "no refresh token available — remove and re-add the integration",
            )
            return False

        try:
            auth = await cognito_refresh(self._session, refresh_token)
        except LoginError as ex:
            self._set_status(
                ConnectivityStatus.EXPIRED_TOKEN,
                f"refresh failed ({ex}) — remove and re-add the integration",
            )
            return False

        new_expires = time.time() + int(auth.get("ExpiresIn", 3600))
        await self._config_manager.update_tokens(
            auth["IdToken"],
            auth.get("RefreshToken"),
            new_expires,
        )
        _LOGGER.debug("Refreshed Cognito IdToken")
        return True

    async def _authenticate_user(self) -> bool:
        try:
            data = await fetch_user_profile(
                self._session, self._config_manager.id_token
            )
        except LoginError as ex:
            self._set_status(ConnectivityStatus.FAILED, f"authenticate-user: {ex}")
            return False

        serial_number = data.get("Sernum")
        motor_unit_serial = data.get(API_RESPONSE_UNIT_SERIAL_NUMBER)

        if serial_number and serial_number != self._config_manager.serial_number:
            await self._config_manager.update_serial_number(serial_number)

        if (
            motor_unit_serial
            and motor_unit_serial != self._config_manager.motor_unit_serial
        ):
            await self._config_manager.update_motor_unit_serial(motor_unit_serial)

        for key, mapped in DATA_ROBOT_DETAILS.items():
            if key in data:
                self.data[mapped] = data.get(key)

        return True

    async def _get_aws_credentials(self):
        try:
            data = await fetch_aws_credentials(
                self._session, self._config_manager.id_token
            )
        except LoginError as ex:
            self._set_status(ConnectivityStatus.FAILED, f"getToken: {ex}")
            return

        for field in API_TOKEN_FIELDS:
            self.data[field] = data.get(field)

        self._set_status(ConnectivityStatus.CONNECTED)

    def _set_status(self, status: ConnectivityStatus, message: str | None = None):
        log_level = ConnectivityStatus.get_log_level(status)

        if status != self._status:
            log_message = f"Status update {self._status} --> {status}"
            if message is not None:
                log_message = f"{log_message}, {message}"
            _LOGGER.log(log_level, log_message)
            self._status = status
            self._async_dispatcher_send(
                SIGNAL_API_STATUS, self._config_manager.entry_id, status
            )
        else:
            log_message = f"Status is {status}"
            if message is not None:
                log_message = f"{log_message}, {message}"
            _LOGGER.log(log_level, log_message)

    def set_local_async_dispatcher_send(self, callback):
        self._local_async_dispatcher_send = callback

    def _async_dispatcher_send(self, signal: str, *args: Any) -> None:
        if self._hass is None:
            self._local_async_dispatcher_send(signal, *args)
        else:
            dispatcher_send(self._hass, signal, *args)
