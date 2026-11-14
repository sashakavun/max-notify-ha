"""Платформа notify: проверка возможностей записи и вызов методов провайдера."""

from __future__ import annotations

from .log import get_logger
import logging
from typing import Any

from homeassistant.components.notify import (
    ATTR_DATA,
    ATTR_MESSAGE,
    ATTR_TITLE,
    DOMAIN as NOTIFY_DOMAIN,
    NOTIFY_SERVICE_SCHEMA,
    NotifyEntity,
    NotifyEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant, ServiceCall, callback, split_entity_id
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.service import async_set_service_schema

try:
    from homeassistant.config_entries import ConfigSubentry
except ImportError:
    class ConfigSubentry:  # type: ignore[too-many-ancestors]
        """Заглушка для старых версий Home Assistant без ConfigSubentry."""

from .const import (
    CONF_COUNT_REQUESTS,
    CONF_DISABLE_SSL,
    CONF_FILES,
    CONF_SEND_KEYBOARD,
    CONF_URL_AUTH_LOGIN,
    CONF_URL_AUTH_PASSWORD,
    CONF_URL_AUTH_TOKEN,
    CONF_URL_AUTH_TYPE,
    DOMAIN,
    SERVICE_SEND_DOCUMENT,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_PHOTO,
    SERVICE_SEND_VIDEO,
)
from .device_helpers import recipient_device_info
from .providers.registry import (
    get_provider,
)

_LOGGER = get_logger()

# Действие notify.<имя сущности> (как у «старых» notify-интеграций): нужно там, где
# получатель задаётся именем действия, а не сущностью — например, в интеграции Alert.
# Ключи data — те же, что у действий max_notify.send_message / send_photo / send_video / send_document.
_NOTIFY_ACTION_MEDIA_SERVICES = {
    "photo": SERVICE_SEND_PHOTO,
    "video": SERVICE_SEND_VIDEO,
    "document": SERVICE_SEND_DOCUMENT,
}
_NOTIFY_ACTION_SEND_KEYS = ("format", "notify", CONF_SEND_KEYBOARD, "buttons")
_NOTIFY_ACTION_MEDIA_KEYS = (
    CONF_COUNT_REQUESTS,
    CONF_DISABLE_SSL,
    CONF_URL_AUTH_TYPE,
    CONF_URL_AUTH_LOGIN,
    CONF_URL_AUTH_PASSWORD,
    CONF_URL_AUTH_TOKEN,
)
_NOTIFY_ACTION_FIELDS = {
    ATTR_MESSAGE: {"required": True, "selector": {"text": None}},
    ATTR_TITLE: {"selector": {"text": None}},
    ATTR_DATA: {"selector": {"object": None}},
}


def _recipient_preview(
    recipient: dict[str, Any], subentry_title: str | None
) -> str:
    """Краткая строка для UI: кто получатель (user/chat/id или заголовок подзаписи)."""
    rid_raw = recipient.get("recipient_id")
    rid: int | None = None
    try:
        rid = int(rid_raw) if rid_raw is not None else None
    except (TypeError, ValueError):
        rid = None
    if rid is not None:
        return f"{'Chat' if rid < 0 else 'User'} {rid}"
    uid = recipient.get("user_id")
    if uid is not None:
        return f"User {uid}"
    cid = recipient.get("chat_id")
    if cid is not None:
        return f"Chat {cid}"
    if isinstance(subentry_title, str) and subentry_title.strip():
        return subentry_title.strip()
    return "Unresolved"


# Вспомогательные функции для тестов и внутренних вызовов (реализация в providers/notify_outbound).
from .providers.notify_outbound import (
    recipient_dict_from_subentry,
    recipient_resolution_from_subentry,
)


async def delete_message(
    hass: HomeAssistant, entry: ConfigEntry, message_id: str
) -> bool:
    provider = get_provider(entry)
    provider.ensure_can_delete_message(hass, entry)
    return await provider.async_delete_message(hass, entry, message_id)


async def delete_messages(
    hass: HomeAssistant, entry: ConfigEntry, message_ids: list[str]
) -> list[str]:
    provider = get_provider(entry)
    provider.ensure_can_delete_message(hass, entry)
    from .providers import notify_outbound

    return await notify_outbound.delete_messages(hass, entry, message_ids)


async def list_message_ids_in_period(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    *,
    ts_from: int | None,
    ts_to: int | None,
) -> list[str]:
    provider = get_provider(entry)
    provider.ensure_can_delete_message(hass, entry)
    provider.ensure_can_delete_message_by_period(hass, entry)
    return await provider.async_list_message_ids_in_period(
        hass,
        entry,
        recipient,
        ts_from=ts_from,
        ts_to=ts_to,
    )


async def edit_message(
    hass: HomeAssistant,
    entry: ConfigEntry,
    message_id: str,
    text: str | None = None,
    buttons: list[list[dict[str, Any]]] | None = None,
    remove_buttons: bool = False,
    format: str | None = None,
) -> bool:
    provider = get_provider(entry)
    provider.ensure_can_edit_message(hass, entry)
    if text is not None:
        provider.ensure_message_format_allowed(hass, entry, format)
    return await provider.async_edit_message(
        hass,
        entry,
        message_id,
        text=text,
        buttons=buttons,
        remove_buttons=remove_buttons,
        format=format,
    )


async def delete_last_outgoing_message(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    *,
    scan_count: int,
) -> bool:
    provider = get_provider(entry)
    provider.ensure_can_delete_last_outgoing_message(hass, entry)
    return await provider.async_delete_last_outgoing_message(
        hass,
        entry,
        recipient,
        scan_count=scan_count,
    )


async def send_message_with_buttons(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    message: str,
    buttons: list[list[dict[str, Any]]],
    title: str | None = None,
    message_format: str | None = None,
    notify: bool = True,
) -> None:
    await send_message(
        hass,
        entry,
        recipient,
        message,
        buttons=buttons,
        title=title,
        message_format=message_format,
        notify=notify,
    )


async def send_message(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    message: str,
    *,
    buttons: list[list[dict[str, Any]]] | None = None,
    title: str | None = None,
    message_format: str | None = None,
    notify: bool = True,
) -> None:
    provider = get_provider(entry)
    provider.ensure_can_send_message(
        hass, entry, recipient, with_buttons=bool(buttons)
    )
    provider.ensure_message_format_allowed(hass, entry, message_format)
    await provider.async_send_message(
        hass,
        entry,
        recipient,
        message,
        buttons=buttons,
        title=title,
        message_format=message_format,
        notify=notify,
    )

async def send_plain_message(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    message: str,
    title: str | None = None,
    message_format: str | None = None,
    notify: bool = True,
) -> None:
    await send_message(
        hass,
        entry,
        recipient,
        message,
        buttons=None,
        title=title,
        message_format=message_format,
        notify=notify,
    )


async def upload_image_and_send(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    file_path_or_url: str,
    file_paths_or_urls: list[str] | None = None,
    caption: str | None = None,
    buttons: list[list[dict[str, Any]]] | None = None,
    count_requests: int | None = None,
    notify: bool = True,
    disable_ssl: bool = False,
    url_auth_type: str | None = None,
    url_auth_login: str | None = None,
    url_auth_password: str | None = None,
    url_auth_token: str | None = None,
    message_format: str | None = None,
) -> None:
    provider = get_provider(entry)
    provider.ensure_can_upload_image(
        hass, entry, recipient, with_buttons=bool(buttons)
    )
    provider.ensure_message_format_allowed(hass, entry, message_format)
    await provider.async_upload_image_and_send(
        hass,
        entry,
        recipient,
        file_path_or_url,
        file_paths_or_urls=file_paths_or_urls,
        caption=caption,
        buttons=buttons,
        count_requests=count_requests,
        notify=notify,
        disable_ssl=disable_ssl,
        url_auth_type=url_auth_type,
        url_auth_login=url_auth_login,
        url_auth_password=url_auth_password,
        url_auth_token=url_auth_token,
        message_format=message_format,
    )

async def upload_document_and_send(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    file_path_or_url: str,
    file_paths_or_urls: list[str] | None = None,
    caption: str | None = None,
    buttons: list[list[dict[str, Any]]] | None = None,
    count_requests: int | None = None,
    notify: bool = True,
    disable_ssl: bool = False,
    url_auth_type: str | None = None,
    url_auth_login: str | None = None,
    url_auth_password: str | None = None,
    url_auth_token: str | None = None,
    message_format: str | None = None,
) -> None:
    provider = get_provider(entry)
    provider.ensure_can_upload_document(
        hass, entry, recipient, with_buttons=bool(buttons)
    )
    provider.ensure_message_format_allowed(hass, entry, message_format)
    await provider.async_upload_document_and_send(
        hass,
        entry,
        recipient,
        file_path_or_url,
        file_paths_or_urls=file_paths_or_urls,
        caption=caption,
        buttons=buttons,
        count_requests=count_requests,
        notify=notify,
        disable_ssl=disable_ssl,
        url_auth_type=url_auth_type,
        url_auth_login=url_auth_login,
        url_auth_password=url_auth_password,
        url_auth_token=url_auth_token,
        message_format=message_format,
    )


async def upload_video_and_send(
    hass: HomeAssistant,
    entry: ConfigEntry,
    recipient: dict[str, Any],
    file_path_or_url: str,
    file_paths_or_urls: list[str] | None = None,
    caption: str | None = None,
    buttons: list[list[dict[str, Any]]] | None = None,
    count_requests: int | None = None,
    notify: bool = True,
    disable_ssl: bool = False,
    url_auth_type: str | None = None,
    url_auth_login: str | None = None,
    url_auth_password: str | None = None,
    url_auth_token: str | None = None,
    message_format: str | None = None,
) -> None:
    provider = get_provider(entry)
    provider.ensure_can_upload_video(
        hass, entry, recipient, with_buttons=bool(buttons)
    )
    provider.ensure_message_format_allowed(hass, entry, message_format)
    await provider.async_upload_video_and_send(
        hass,
        entry,
        recipient,
        file_path_or_url,
        file_paths_or_urls=file_paths_or_urls,
        caption=caption,
        buttons=buttons,
        count_requests=count_requests,
        notify=notify,
        disable_ssl=disable_ssl,
        url_auth_type=url_auth_type,
        url_auth_login=url_auth_login,
        url_auth_password=url_auth_password,
        url_auth_token=url_auth_token,
        message_format=message_format,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    _LOGGER.debug("async_setup_entry: запись=%s", entry.entry_id)

    subentries = getattr(entry, "subentries", None) or {}
    entities: list[MaxNotifyEntity] = []
    for subentry_id, subentry in subentries.items():
        if not isinstance(subentry, ConfigSubentry):
            continue
        recipient, recipient_meta = recipient_resolution_from_subentry(
            subentry, hass=hass, entry_id=entry.entry_id
        )
        entity = MaxNotifyEntity(
            hass,
            entry,
            recipient=recipient,
            subentry=subentry,
            recipient_meta=recipient_meta,
        )
        _LOGGER.debug(
            "Добавление сущности notify из чата %s: %s", subentry_id, entity.name
        )
        entities.append((entity, subentry_id))
    if not entities:
        return
    for entity, subentry_id in entities:
        async_add_entities([entity], config_subentry_id=subentry_id)


class MaxNotifyEntity(NotifyEntity):
    """Представление сущности MaxNotify."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    # Заголовок поддерживается (уходит первой строкой) — поле title видно в notify.send_message.
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        recipient: dict[str, Any],
        subentry: ConfigSubentry,
        recipient_meta: dict[str, Any] | None = None,
    ) -> None:
        self._entry = entry
        self._recipient = recipient
        self.subentry = subentry
        self._recipient_meta = dict(recipient_meta or {})
        # Имя зарегистрированного действия notify.<имя сущности> (None — не зарегистрировано).
        self._notify_action: str | None = None
        self._attr_unique_id = f"{entry.entry_id}_{subentry.subentry_id}"
        # Имя сущности = имя device чата (как telegram_bot notify).
        self._attr_name = None
        self._attr_device_info = recipient_device_info(hass, entry, subentry)
        rid_raw = recipient.get("recipient_id")
        rid: int | None = None
        try:
            rid = int(rid_raw) if rid_raw is not None else None
        except (TypeError, ValueError):
            rid = None
        rtype = "chat" if rid is not None and rid < 0 else "user" if rid else "unknown"
        self._attr_extra_state_attributes = {
            "integration_config_path": f"/config/integrations/integration/{entry.entry_id}",
            "recipient_preview": _recipient_preview(recipient, subentry.title),
            "recipient_id": rid,
            "recipient_type": rtype,
            "resolved_user_id": recipient.get("user_id"),
            "resolved_chat_id": recipient.get("chat_id"),
            "recipient_resolution_source": self._recipient_meta.get("source"),
            "recipient_storage_key": self._recipient_meta.get("storage_key"),
            "stored_recipient_id": self._recipient_meta.get("stored_recipient_id"),
            "subentry_id": self._recipient_meta.get("subentry_id"),
            "subentry_unique_id": self._recipient_meta.get("subentry_unique_id"),
            "subentry_title": self._recipient_meta.get("subentry_title"),
            "subentry_data_raw": self._recipient_meta.get("subentry_data"),
        }

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        provider = get_provider(self._entry)
        provider.ensure_can_send_message(
            self.hass, self._entry, self._recipient, with_buttons=False
        )
        provider.ensure_message_format_allowed(self.hass, self._entry, None)
        await provider.async_entity_send_plain_message(
            self.hass,
            self._entry,
            self._recipient,
            message,
            title,
            notify=True,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._async_register_notify_action()

    async def async_will_remove_from_hass(self) -> None:
        self._async_unregister_notify_action()
        await super().async_will_remove_from_hass()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Сущность переименовали: действие notify.<имя> переезжает на новое имя.

        Хук новых версий Home Assistant; на старых сущность при переименовании
        удаляется и добавляется заново (async_will_remove_from_hass / async_added_to_hass).
        """
        super().async_entity_id_changed(old_entity_id)
        self._async_unregister_notify_action()
        self._async_register_notify_action()

    @callback
    def _async_register_notify_action(self) -> None:
        """Зарегистрировать действие notify.<имя сущности> (чужое с тем же именем не трогаем)."""
        action = split_entity_id(self.entity_id)[1]
        if self.hass.services.has_service(NOTIFY_DOMAIN, action):
            _LOGGER.warning(
                "Действие %s.%s уже занято, для сущности %s оно не создано",
                NOTIFY_DOMAIN,
                action,
                self.entity_id,
            )
            return
        self.hass.services.async_register(
            NOTIFY_DOMAIN,
            action,
            self._async_handle_notify_action,
            schema=NOTIFY_SERVICE_SCHEMA,
        )
        async_set_service_schema(
            self.hass,
            NOTIFY_DOMAIN,
            action,
            {
                "name": f"Send a notification via {action}",
                "description": (
                    f"Sends a message to the Max chat of {self.entity_id} (MaxNotify)."
                ),
                "fields": _NOTIFY_ACTION_FIELDS,
            },
        )
        self._notify_action = action
        _LOGGER.debug(
            "Зарегистрировано действие %s.%s для сущности %s",
            NOTIFY_DOMAIN,
            action,
            self.entity_id,
        )

    @callback
    def _async_unregister_notify_action(self) -> None:
        """Снять действие notify.<имя сущности>, если его регистрировала эта сущность."""
        action = self._notify_action
        if action is None:
            return
        self._notify_action = None
        self.hass.services.async_remove(NOTIFY_DOMAIN, action)
        _LOGGER.debug("Снято действие %s.%s", NOTIFY_DOMAIN, action)

    async def _async_handle_notify_action(self, service: ServiceCall) -> None:
        """Обработка notify.<имя сущности>: message, title и необязательный data.

        Без известных ключей в data — то же, что notify.send_message для этой сущности.
        С ними — соответствующее действие max_notify.* для этого чата. Незнакомые ключи
        data пропускаются: в Alert блок data общий для всех получателей.
        """
        message = service.data[ATTR_MESSAGE]
        title = service.data.get(ATTR_TITLE)
        data = service.data.get(ATTR_DATA) or {}

        media = [key for key in _NOTIFY_ACTION_MEDIA_SERVICES if key in data]
        if len(media) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="notify_action_one_attachment_type",
            )
        options = {key: data[key] for key in _NOTIFY_ACTION_SEND_KEYS if key in data}
        _LOGGER.debug(
            "%s.%s: длина_текста=%s есть_заголовок=%s вложение=%s параметры=%s ключи_data=%s",
            NOTIFY_DOMAIN,
            service.service,
            len(message),
            bool(title),
            media[0] if media else None,
            sorted(options),
            sorted(str(key) for key in data),
        )

        if not media and not options:
            await self._async_send_message(message=message, title=title)
            return

        # Клавиатура из настроек записи — только по явному send_keyboard: true.
        payload: dict[str, Any] = {
            ATTR_ENTITY_ID: [self.entity_id],
            CONF_SEND_KEYBOARD: False,
            **options,
        }
        if media:
            target_service = _NOTIFY_ACTION_MEDIA_SERVICES[media[0]]
            source = data[media[0]]
            if target_service == SERVICE_SEND_DOCUMENT and isinstance(source, list):
                if len(source) != 1:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="service_send_document_one_file_only",
                    )
                source = source[0]
            payload[CONF_FILES if isinstance(source, list) else "file"] = source
            caption = f"{title}\n{message}" if title else message
            if caption:
                payload["caption"] = caption
            payload.update(
                {key: data[key] for key in _NOTIFY_ACTION_MEDIA_KEYS if key in data}
            )
        else:
            target_service = SERVICE_SEND_MESSAGE
            payload["message"] = message
            if title:
                payload["title"] = title

        await self.hass.services.async_call(
            DOMAIN,
            target_service,
            payload,
            blocking=True,
            context=service.context,
        )
        self._async_record_notification()
