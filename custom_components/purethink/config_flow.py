import logging
import re

import voluptuous as vol
from homeassistant import config_entries

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class PurethinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """설정 마법사 클래스"""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """사용자 설정 단계"""
        _LOGGER.debug("Starting config flow")
        errors = {}
        if user_input is not None:
            _LOGGER.debug(f"User input received: {user_input}")

            # 유효성 검사
            if len(user_input.get("friendly_name", "")) > 30:
                errors["base"] = "name_too_long"
                _LOGGER.warning("Name too long: %s", user_input["friendly_name"])
            elif not user_input.get("friendly_name", "").strip():
                errors["base"] = "name_required"
                _LOGGER.warning("Empty name field")
            else:
                # 엔터티 기본 ID 생성
                base_id = re.sub(r'[^\w]', '_',
                                 user_input["friendly_name"].lower().replace(" ", "_"))
                _LOGGER.info(f"Creating entry with base_id: {base_id}")

                return self.async_create_entry(
                    title=user_input["friendly_name"],
                    data={
                        "friendly_name": user_input["friendly_name"],
                        "device_id": user_input["device_id"],
                        "base_id": base_id
                    }
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("friendly_name",
                             description={"placeholder": "예: 거실 환기청정기"}): str,
                vol.Required("device_id",
                             description={"placeholder": "제품 하단의 ID (예: DIV01-AB1234)"}): str
            }),
            errors=errors
        )
