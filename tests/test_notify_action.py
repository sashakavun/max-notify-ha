"""Тесты действия notify.<имя сущности> (прямой вызов чата, например из Alert)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.notify import (
    NOTIFY_SERVICE_SCHEMA,
    NotifyEntity,
    NotifyEntityFeature,
)
from homeassistant.exceptions import ServiceValidationError

from custom_components.max_notify.const import (
    DOMAIN,
    SERVICE_SEND_DOCUMENT,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_PHOTO,
    SERVICE_SEND_VIDEO,
)
from custom_components.max_notify.notify import MaxNotifyEntity

ENTITY_ID = "notify.ivan"


@pytest.fixture
def entity(hass, mock_config_entry):
    """Сущность чата с mock-реестром действий Home Assistant."""
    hass.services.has_service = MagicMock(return_value=False)
    hass.services.async_register = MagicMock()
    hass.services.async_remove = MagicMock()
    hass.services.async_call = AsyncMock()
    subentry = MagicMock()
    subentry.subentry_id = "sub-1"
    subentry.title = "Ivan"
    ent = MaxNotifyEntity(
        hass,
        mock_config_entry,
        recipient={"recipient_id": 111},
        subentry=subentry,
    )
    ent.hass = hass
    ent.entity_id = ENTITY_ID
    with (
        patch("custom_components.max_notify.notify.async_set_service_schema"),
        patch.object(MaxNotifyEntity, "_async_send_message", new=AsyncMock()),
        patch.object(MaxNotifyEntity, "_async_record_notification", new=MagicMock()),
    ):
        yield ent


def _call(
    message: str = "hello",
    title: str | None = None,
    data: dict | None = None,
) -> MagicMock:
    """Вызов действия после проверки стандартной схемой notify."""
    payload: dict = {"message": message}
    if title is not None:
        payload["title"] = title
    if data is not None:
        payload["data"] = data
    call = MagicMock()
    call.service = "ivan"
    call.data = NOTIFY_SERVICE_SCHEMA(payload)
    return call


class TestEntityFeatures:
    """Возможности сущности, которые видит notify.send_message."""

    def test_title_supported(self, entity) -> None:
        assert entity.supported_features & NotifyEntityFeature.TITLE


@pytest.mark.asyncio
class TestNotifyActionRegistration:
    """Регистрация и снятие действия notify.<имя сущности>."""

    async def test_registered_when_entity_added(self, hass, entity) -> None:
        await entity.async_added_to_hass()

        hass.services.async_register.assert_called_once()
        args, kwargs = hass.services.async_register.call_args
        assert args[:2] == ("notify", "ivan")
        assert args[2] == entity._async_handle_notify_action
        assert kwargs["schema"] is NOTIFY_SERVICE_SCHEMA

    async def test_description_for_ui(self, hass, entity) -> None:
        with patch(
            "custom_components.max_notify.notify.async_set_service_schema"
        ) as set_schema:
            await entity.async_added_to_hass()

        args = set_schema.call_args.args
        assert args[:3] == (hass, "notify", "ivan")
        assert set(args[3]["fields"]) == {"message", "title", "data"}

    async def test_removed_with_entity(self, hass, entity) -> None:
        await entity.async_added_to_hass()
        await entity.async_will_remove_from_hass()

        hass.services.async_remove.assert_called_once_with("notify", "ivan")

    async def test_busy_name_is_not_overwritten(self, hass, entity) -> None:
        hass.services.has_service.return_value = True

        await entity.async_added_to_hass()
        await entity.async_will_remove_from_hass()

        hass.services.has_service.assert_called_once_with("notify", "ivan")
        hass.services.async_register.assert_not_called()
        hass.services.async_remove.assert_not_called()

    async def test_remove_without_registration_is_noop(self, hass, entity) -> None:
        await entity.async_will_remove_from_hass()

        hass.services.async_remove.assert_not_called()

    async def test_rename_by_readd(self, hass, entity) -> None:
        """Старые версии HA: при переименовании сущность удаляется и добавляется заново."""
        await entity.async_added_to_hass()
        await entity.async_will_remove_from_hass()
        entity.entity_id = "notify.max_ivan"
        await entity.async_added_to_hass()

        hass.services.async_remove.assert_called_once_with("notify", "ivan")
        assert hass.services.async_register.call_args.args[:2] == ("notify", "max_ivan")

    @pytest.mark.skipif(
        not hasattr(NotifyEntity, "async_entity_id_changed"),
        reason="Хук async_entity_id_changed есть только в новых версиях Home Assistant",
    )
    async def test_rename_by_hook(self, hass, entity) -> None:
        """Новые версии HA: сущность остаётся, вызывается async_entity_id_changed."""
        await entity.async_added_to_hass()
        entity.entity_id = "notify.max_ivan"
        entity.async_entity_id_changed(ENTITY_ID)

        hass.services.async_remove.assert_called_once_with("notify", "ivan")
        assert hass.services.async_register.call_count == 2
        assert hass.services.async_register.call_args.args[:2] == ("notify", "max_ivan")


@pytest.mark.asyncio
class TestNotifyActionHandler:
    """Обработка вызова notify.<имя сущности>."""

    async def test_without_data_same_as_entity(self, hass, entity) -> None:
        await entity._async_handle_notify_action(_call("hello", title="Title"))

        entity._async_send_message.assert_awaited_once_with(
            message="hello", title="Title"
        )
        hass.services.async_call.assert_not_called()

    async def test_unknown_data_keys_are_ignored(self, hass, entity) -> None:
        """В Alert блок data общий для всех получателей — чужие ключи не мешают."""
        await entity._async_handle_notify_action(
            _call(data={"push": {"sound": "default"}, "ttl": 0})
        )

        entity._async_send_message.assert_awaited_once_with(message="hello", title=None)
        hass.services.async_call.assert_not_called()

    async def test_send_options_go_to_send_message(self, hass, entity) -> None:
        call = _call("*hi*", title="Title", data={"format": "markdown", "notify": False})

        await entity._async_handle_notify_action(call)

        hass.services.async_call.assert_awaited_once_with(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "entity_id": [ENTITY_ID],
                "send_keyboard": False,
                "format": "markdown",
                "notify": False,
                "message": "*hi*",
                "title": "Title",
            },
            blocking=True,
            context=call.context,
        )
        entity._async_send_message.assert_not_called()
        entity._async_record_notification.assert_called_once()

    async def test_keyboard_only_on_request(self, hass, entity) -> None:
        await entity._async_handle_notify_action(_call(data={"send_keyboard": True}))
        payload = hass.services.async_call.await_args.args[2]
        assert payload["send_keyboard"] is True

        await entity._async_handle_notify_action(
            _call(data={"buttons": {"Да": "yes", "Нет": "no"}})
        )
        payload = hass.services.async_call.await_args.args[2]
        assert payload["send_keyboard"] is False
        assert payload["buttons"] == {"Да": "yes", "Нет": "no"}

    async def test_photo_with_caption_from_title_and_message(self, hass, entity) -> None:
        await entity._async_handle_notify_action(
            _call("Открыта дверь", title="Тревога", data={"photo": "/config/www/a.jpg"})
        )

        args = hass.services.async_call.await_args.args
        assert args[:2] == (DOMAIN, SERVICE_SEND_PHOTO)
        assert args[2] == {
            "entity_id": [ENTITY_ID],
            "send_keyboard": False,
            "file": "/config/www/a.jpg",
            "caption": "Тревога\nОткрыта дверь",
        }
        entity._async_record_notification.assert_called_once()

    async def test_video_list_and_media_options(self, hass, entity) -> None:
        await entity._async_handle_notify_action(
            _call(
                "",
                data={
                    "video": ["https://cam.local/a.mp4", "/media/b.mp4"],
                    "notify": False,
                    "count_requests": 3,
                    "disable_ssl": True,
                    "url_auth_type": "bearer",
                    "url_auth_token": "secret",
                },
            )
        )

        args = hass.services.async_call.await_args.args
        assert args[:2] == (DOMAIN, SERVICE_SEND_VIDEO)
        assert args[2] == {
            "entity_id": [ENTITY_ID],
            "send_keyboard": False,
            "notify": False,
            "files": ["https://cam.local/a.mp4", "/media/b.mp4"],
            "count_requests": 3,
            "disable_ssl": True,
            "url_auth_type": "bearer",
            "url_auth_token": "secret",
        }

    async def test_media_options_without_attachment_are_ignored(
        self, hass, entity
    ) -> None:
        await entity._async_handle_notify_action(_call(data={"disable_ssl": True}))

        entity._async_send_message.assert_awaited_once()
        hass.services.async_call.assert_not_called()

    async def test_document_single_file(self, hass, entity) -> None:
        await entity._async_handle_notify_action(
            _call("Отчёт", data={"document": ["/config/report.pdf"]})
        )

        args = hass.services.async_call.await_args.args
        assert args[:2] == (DOMAIN, SERVICE_SEND_DOCUMENT)
        assert args[2]["file"] == "/config/report.pdf"
        assert args[2]["caption"] == "Отчёт"
        assert "files" not in args[2]

    async def test_document_rejects_several_files(self, hass, entity) -> None:
        with pytest.raises(ServiceValidationError) as exc:
            await entity._async_handle_notify_action(
                _call(data={"document": ["/config/a.pdf", "/config/b.pdf"]})
            )

        assert exc.value.translation_key == "service_send_document_one_file_only"
        hass.services.async_call.assert_not_called()

    async def test_only_one_attachment_type(self, hass, entity) -> None:
        with pytest.raises(ServiceValidationError) as exc:
            await entity._async_handle_notify_action(
                _call(data={"photo": "/config/a.jpg", "video": "/config/b.mp4"})
            )

        assert exc.value.translation_key == "notify_action_one_attachment_type"
        hass.services.async_call.assert_not_called()
        entity._async_send_message.assert_not_called()

    async def test_send_error_is_raised_and_state_not_updated(
        self, hass, entity
    ) -> None:
        hass.services.async_call.side_effect = ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="send_message_failed_after_retries",
        )

        with pytest.raises(ServiceValidationError):
            await entity._async_handle_notify_action(_call(data={"notify": False}))

        entity._async_record_notification.assert_not_called()
