import json
import requests
from django.utils import timezone

from django import forms
from django.template.defaultfilters import truncatechars

from snappea.decorators import shared_task
from bugsink.app_settings import get_settings
from bugsink.transaction import immediate_atomic

from issues.models import Issue


class TelegramConfigForm(forms.Form):
    bot_token = forms.CharField(required=True)
    chat_id = forms.CharField(required=True, help_text="Use @channelname for channels or a numeric chat ID.")

    def __init__(self, *args, **kwargs):
        config = kwargs.pop("config", None)

        super().__init__(*args, **kwargs)
        if config:
            self.fields["bot_token"].initial = config.get("bot_token", "")
            self.fields["chat_id"].initial = config.get("chat_id", "")

    def get_config(self):
        return {
            "bot_token": self.cleaned_data.get("bot_token"),
            "chat_id": self.cleaned_data.get("chat_id"),
        }


def _store_failure_info(service_config_id, exception, response=None):
    """Store failure information in the MessagingServiceConfig with immediate_atomic"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)

            config.last_failure_timestamp = timezone.now()
            config.last_failure_error_type = type(exception).__name__
            config.last_failure_error_message = str(exception)

            # Handle requests-specific errors
            if response is not None:
                config.last_failure_status_code = response.status_code
                config.last_failure_response_text = response.text[:2000]  # Limit response text size

                # Check if response is JSON
                try:
                    json.loads(response.text)
                    config.last_failure_is_json = True
                except (json.JSONDecodeError, ValueError):
                    config.last_failure_is_json = False
            else:
                # Non-HTTP errors
                config.last_failure_status_code = None
                config.last_failure_response_text = None
                config.last_failure_is_json = None

            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


def _store_success_info(service_config_id):
    """Clear failure information on successful operation"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)
            config.clear_failure_status()
            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


def _send_telegram_message(bot_token, payload, service_config_id):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    try:
        result = requests.post(url, json=payload, timeout=5)
        try:
            response_data = result.json()
        except ValueError:
            response_data = None

        if result.status_code >= 400:
            result.raise_for_status()

        if response_data is not None and response_data.get("ok") is False:
            error = requests.HTTPError(
                response_data.get("description", "Telegram API error")
            )
            error.response = result
            raise error

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, "response", None)
        _store_failure_info(service_config_id, e, response)
    except Exception as e:
        _store_failure_info(service_config_id, e)


def _truncate_message(text, max_length=4000):
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


@shared_task
def telegram_backend_send_test_message(
    bot_token, chat_id, project_name, display_name, service_config_id
):
    lines = [
        "Test message by Bugsink to test the Telegram bot setup.",
        f"Project: {project_name}",
        f"Message backend: {display_name}",
    ]
    payload = {
        "chat_id": chat_id,
        "text": _truncate_message("\n".join(lines)),
        "disable_web_page_preview": True,
    }

    _send_telegram_message(bot_token, payload, service_config_id)


@shared_task
def telegram_backend_send_alert(
    bot_token,
    chat_id,
    issue_id,
    state_description,
    alert_article,
    alert_reason,
    service_config_id,
    unmute_reason=None,
):
    issue = Issue.objects.get(id=issue_id)
    issue_url = get_settings().BASE_URL + issue.get_absolute_url()

    lines = [
        f"{alert_reason} issue",
        f"Issue: {truncatechars(issue.title(), 200)}",
        f"Project: {issue.project.name}",
        f"URL: {issue_url}",
    ]
    if unmute_reason:
        lines.append(f"Unmute reason: {unmute_reason}")

    payload = {
        "chat_id": chat_id,
        "text": _truncate_message("\n".join(lines)),
        "disable_web_page_preview": True,
    }

    _send_telegram_message(bot_token, payload, service_config_id)


class TelegramBackend:
    def __init__(self, service_config):
        self.service_config = service_config

    @classmethod
    def get_form_class(cls):
        return TelegramConfigForm

    def send_test_message(self):
        config = json.loads(self.service_config.config)
        telegram_backend_send_test_message.delay(
            config["bot_token"],
            config["chat_id"],
            self.service_config.project.name,
            self.service_config.display_name,
            self.service_config.id,
        )

    def send_alert(self, issue_id, state_description, alert_article, alert_reason, **kwargs):
        config = json.loads(self.service_config.config)
        telegram_backend_send_alert.delay(
            config["bot_token"],
            config["chat_id"],
            issue_id,
            state_description,
            alert_article,
            alert_reason,
            self.service_config.id,
            **kwargs,
        )
