import inspect
from datetime import datetime
from html import escape

import environ
import httpx
import pymysql
import requests
import urllib3
from django.conf import settings
from openai import OpenAI

from helpers.config import base_forecast_logger as ai_forecast_logger


env = environ.Env()
environ.Env.read_env()

OPENAI_CLIENT_ID = None
OPENAI_PROJECT_ID = None
OPENAI_KEY_TRACKING_ID = None
MAIL_SENT = False


def convert_forecast_month_to_date(month_str):
    """Convert a forecast month label such as 'Feb-26' to '2026-02-01'."""
    if not month_str:
        return None
    try:
        dt = datetime.strptime(str(month_str).strip(), "%b-%y")
        return dt.strftime("%Y-%m-01")
    except (TypeError, ValueError):
        ai_forecast_logger.warning("[FORECAST DATE] Invalid month label: %r", month_str)
        return None


def load_openai_key_from_db(client_id=None):
    """Load the active OpenAI key/configuration for the AI Forecast workload."""
    db_connection = None
    row = None

    try:
        mode = settings.MODE

        if mode == "DEV1":
            db_name = env("MYSQL_DATABASE_NAME")
            db_user = env("MYSQL_DATABASE_USER")
            db_password = env("MYSQL_DATABASE_PASSWORD")
            db_host = env("MYSQL_DATABASE_HOST")
            db_port = int(env("MYSQL_DATABASE_PORT"))
        elif mode in {"DEV", "QA", "TESTING", "PRODUCTION"}:
            db_name = env("AMPRO_DATABASE_NAME")
            db_user = env("AMPRO_DATABASE_USER")
            db_password = env("AMPRO_DATABASE_PASSWORD")
            db_host = env("AMPRO_DATABASE_HOST")
            db_port = int(env("AMPRO_DATABASE_PORT"))
        else:
            raise ValueError(f"Unsupported MODE: {mode}")

        db_connection = pymysql.connect(
            host=db_host,
            user=db_user,
            password=db_password,
            database=db_name,
            port=db_port,
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=20,
        )

        configured_client_id = client_id or getattr(
            settings, "AI_FORECAST_OPENAI_CLIENT_ID", 1
        )
        intelligence_type_id = getattr(
            settings, "AI_FORECAST_INTELLIGENCE_TYPE_ID", "100001"
        )
        key_name = getattr(settings, "AI_FORECAST_OPENAI_KEY_NAME", "AI Forecast")

        with db_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    OpenAIKey,
                    ClientID,
                    ProjectIdOpenAI,
                    KeyTrackingIDOpenAI
                FROM clientopenaiKeys
                WHERE ClientID = %s
                  AND IsActive = 1
                  AND IntelligenceTypeId = %s
                  AND KeyNameOpenAI = %s
                ORDER BY RowId DESC
                LIMIT 1
                """,
                [configured_client_id, intelligence_type_id, key_name],
            )
            row = cursor.fetchone()

        if not row:
            raise ValueError(
                "No active OpenAI key record found for AI Forecast "
                f"(ClientID={configured_client_id})"
            )

        if not row[0]:
            ai_forecast_logger.error(
                "[OPENAI KEY EMPTY] client_id=%s project_id=%s tracking_id=%s",
                row[1],
                row[2],
                row[3],
            )

        return {
            "openai_key": row[0] or "",
            "client_id": row[1],
            "project_id_openai": row[2],
            "key_tracking_id_openai": row[3],
        }

    except Exception as exc:
        ai_forecast_logger.exception("[OPENAI KEY LOAD ERROR] %s", exc)
        return None

    finally:
        if db_connection:
            try:
                db_connection.close()
            except Exception:
                ai_forecast_logger.exception("[OPENAI KEY DB CLOSE ERROR]")


def get_openai_client(client_id=None):
    """Build the OpenAI client and retain non-secret metadata for error reporting."""
    global OPENAI_CLIENT_ID, OPENAI_PROJECT_ID, OPENAI_KEY_TRACKING_ID

    try:
        key_data = load_openai_key_from_db(client_id)
        if not key_data or not key_data.get("openai_key"):
            ai_forecast_logger.warning("[OPENAI] No usable API key available")
            return None

        OPENAI_CLIENT_ID = key_data["client_id"]
        OPENAI_PROJECT_ID = key_data["project_id_openai"]
        OPENAI_KEY_TRACKING_ID = key_data["key_tracking_id_openai"]

        ai_forecast_logger.info(
            "[OPENAI KEY] Loaded key ending with: ****%s",
            key_data["openai_key"][-4:],
        )

        return OpenAI(
            api_key=key_data["openai_key"],
            timeout=httpx.Timeout(
                connect=float(getattr(settings, "AI_FORECAST_OPENAI_CONNECT_TIMEOUT", 10.0)),
                read=float(getattr(settings, "AI_FORECAST_OPENAI_READ_TIMEOUT", 900.0)),
                write=float(getattr(settings, "AI_FORECAST_OPENAI_WRITE_TIMEOUT", 30.0)),
                pool=float(getattr(settings, "AI_FORECAST_OPENAI_POOL_TIMEOUT", 10.0)),
            ),
        )
    except Exception as exc:
        ai_forecast_logger.exception("[OPENAI CLIENT ERROR] %s", exc)
        return None


def _safe_usage_value(usage, name):
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def _response_incomplete_reason(response):
    details = getattr(response, "incomplete_details", None)
    if details is None:
        return None
    if isinstance(details, dict):
        return details.get("reason")
    return getattr(details, "reason", None)


def create_openai_response(
    client,
    final_prompt,
    MODEL_NAME,
    REASONING_LEVEL,
    *,
    max_output_tokens=8000,
    verbosity="medium",
    enable_web_search=True,
    search_context_size="medium",
    call_name="ai_forecast",
):
    """
    Create a Responses API call with consistent diagnostics and incomplete-response handling.

    A non-completed response is treated as a failure rather than allowing callers to persist
    partial output_text. Callers can tune output budget, verbosity, reasoning and web search
    independently for forecast and summary generation.
    """
    global MAIL_SENT

    if not client:
        ai_forecast_logger.error("[OPENAI] Client not initialized | call=%s", call_name)
        _notify_ai_failure(
            error_msg=f"OpenAI client initialization failed ({call_name})",
            request_id=None,
        )
        return None

    try:
        request_kwargs = {
            "model": MODEL_NAME,
            "input": final_prompt,
            "text": {
                "format": {"type": "text"},
                "verbosity": verbosity,
            },
            "reasoning": {"effort": REASONING_LEVEL},
            "max_output_tokens": max_output_tokens,
            "store": False,
        }

        if enable_web_search:
            request_kwargs["tools"] = [
                {
                    "type": "web_search",
                    "user_location": {"type": "approximate"},
                    "search_context_size": search_context_size,
                }
            ]
            request_kwargs["include"] = ["web_search_call.action.sources"]

        response = client.responses.create(**request_kwargs)

        usage = getattr(response, "usage", None)
        status = getattr(response, "status", None)
        incomplete_reason = _response_incomplete_reason(response)
        response_id = getattr(response, "id", None)

        output_details = _safe_usage_value(usage, "output_tokens_details")
        reasoning_tokens = _safe_usage_value(output_details, "reasoning_tokens")

        ai_forecast_logger.info(
            "[OPENAI RESPONSE] call=%s id=%s status=%s input_tokens=%s "
            "output_tokens=%s reasoning_tokens=%s total_tokens=%s incomplete_reason=%s",
            call_name,
            response_id,
            status,
            _safe_usage_value(usage, "input_tokens"),
            _safe_usage_value(usage, "output_tokens"),
            reasoning_tokens,
            _safe_usage_value(usage, "total_tokens"),
            incomplete_reason,
        )

        # Current Responses API success status is "completed". Some SDK versions may not
        # expose status; in that case preserve compatibility and let the caller validate output.
        if status is not None and status != "completed":
            ai_forecast_logger.error(
                "[OPENAI INCOMPLETE] call=%s id=%s status=%s reason=%s",
                call_name,
                response_id,
                status,
                incomplete_reason or "unknown",
            )
            _notify_ai_failure(
                error_msg=(
                    f"OpenAI response incomplete ({call_name}): "
                    f"status={status} reason={incomplete_reason or 'unknown'}"
                ),
                request_id=response_id,
            )
            return None

        output_text = getattr(response, "output_text", None)
        if not output_text or not str(output_text).strip():
            ai_forecast_logger.error(
                "[OPENAI EMPTY OUTPUT] call=%s id=%s", call_name, response_id
            )
            _notify_ai_failure(
                error_msg=f"OpenAI returned empty output ({call_name})",
                request_id=response_id,
            )
            return None

        return response

    except Exception as exc:
        ai_forecast_logger.exception(
            "[OPENAI RESPONSE ERROR] call=%s error=%s", call_name, exc
        )

        _notify_ai_failure(
            error_msg=f"OpenAI response failed ({call_name}): {exc}",
            request_id=None,
        )

        return None


def _notify_ai_failure(*, error_msg, request_id=None):
    """Send one Amplifi Pro failure mail per job, and retry if the first send failed."""
    global MAIL_SENT
    if MAIL_SENT:
        return
    response = send_ai_failure_mail(
        error_msg=error_msg,
        client_id=OPENAI_CLIENT_ID,
        project_id_openai=OPENAI_PROJECT_ID,
        key_tracking_id_openai=OPENAI_KEY_TRACKING_ID,
        request_id=request_id,
    )
    if response is not None and getattr(response, "ok", False):
        MAIL_SENT = True


def _kwargs_for_callable(func, values):
    """Pass only parameters the target function accepts, filling required Amplifi fields."""
    params = inspect.signature(func).parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return dict(values)

    kwargs = {name: values[name] for name in params if name in values}
    timestamp = values.get("header_date") or values.get("timestamp") or ""
    for name, param in params.items():
        if name in kwargs:
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        if name in {"header_date", "timestamp"}:
            kwargs[name] = timestamp
        else:
            kwargs[name] = values.get(name, "N/A")
    return kwargs


WEBSITE_DISPLAY_TEXT = (
    "www.wnsprocurement"
    "&#8203;"
    ".com"
)


def _build_email_html(
    client_id,
    client_name,
    intelligence_type_id,
    project_id_openai,
    key_tracking_id_openai,
    failure_reason,
    request_id,
    environment,
    fallback_status,
    header_date,
    timestamp,
):
    """
    AmplifiPRO standard OpenAI-key failure email (Story 75173 / Category Insight layout).
    The website line is plain text so Cisco Secure Email cannot wrap it into
    secure-web.cisco.com. API keys are never included.
    """
    status_color_map = {
        "Success": "#28a745",
        "Failed": "#dc3545",
        "Not Applicable": "#6c757d",
    }
    status_color = status_color_map.get(fallback_status, "#6c757d")

    client_id = escape(str(client_id))
    client_name = escape(str(client_name or "N/A"))
    intelligence_type_id = escape(str(intelligence_type_id))
    project_id_openai = escape(str(project_id_openai))
    key_tracking_id_openai = escape(str(key_tracking_id_openai))
    failure_reason = escape(str(failure_reason))
    request_id = escape(str(request_id))
    environment = escape(str(environment))
    fallback_status = escape(str(fallback_status))
    header_date = escape(str(header_date))
    timestamp = escape(str(timestamp))

    return f"""
    <table width="620" border="0" cellpadding="0" cellspacing="0" align="center">
        <tbody>
            <tr style="background-color:#fff;">
                <td colspan="3">
                    <img src="https://dhtfm98jq31zo.cloudfront.net/AmplifiPro_UAT/Web/V2/Logo/email-header-new1.jpg"
                        width="620" height="200" alt="AmplifiPRO">
                </td>
            </tr>
            <tr>
                <td colspan="3" style="padding:15px 0;">
                    <p style="font-size:20px; color:#9abac4; margin:0;
                              font-family:Arial,Helvetica,sans-serif;">
                        <strong>Client OpenAI key failure notification</strong>
                    </p>
                </td>
            </tr>
            <tr>
                <td style="font-family:Arial,Helvetica,sans-serif; font-size:14px;">
                    Hi Team,
                </td>
                <td width="50%" align="right" style="font-family:Arial,Helvetica,sans-serif; font-size:14px;">
                    {header_date}
                </td>
            </tr>
            <tr><td style="text-align:left; vertical-align:top;" colspan="3">&nbsp;</td></tr>
            <tr>
                <td colspan="3" style="padding:15px 0; font-family:Arial,Helvetica,sans-serif; font-size:14px;">
                        Request execution failed due to an issue with the OpenAI key
                        mapping/configuration. Please review the details below and
                        take necessary action.
                        <br><br>
                        <table width="100%" border="0" cellpadding="8" cellspacing="0"
                               style="border-collapse:collapse;
                                      font-family:Arial,Helvetica,sans-serif;
                                      font-size:13px;">
                            <thead>
                                <tr style="background-color:#9abac4; color:#ffffff;">
                                    <th align="left"
                                        style="padding:10px; border:1px solid #ddd; width:45%;">
                                        Field
                                    </th>
                                    <th align="left"
                                        style="padding:10px; border:1px solid #ddd; width:55%;">
                                        Value
                                    </th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr style="background-color:#f9f9f9;">
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Client ID</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{client_id}</td>
                                </tr>
                                <tr>
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Client Name</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{client_name}</td>
                                </tr>
                                <tr style="background-color:#f9f9f9;">
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Execution Type</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{intelligence_type_id}</td>
                                </tr>
                                <tr>
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>OpenAI Project ID</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{project_id_openai}</td>
                                </tr>
                                <tr style="background-color:#f9f9f9;">
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Key Tracking ID</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{key_tracking_id_openai}</td>
                                </tr>
                                <tr>
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Failure Reason</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd; color:#dc3545;">
                                        {failure_reason}
                                    </td>
                                </tr>
                                <tr style="background-color:#f9f9f9;">
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Request ID / Correlation ID</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{request_id}</td>
                                </tr>
                                <tr>
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Environment</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{environment}</td>
                                </tr>
                                <tr style="background-color:#f9f9f9;">
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Timestamp</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">{timestamp}</td>
                                </tr>
                                <tr>
                                    <td style="padding:8px; border:1px solid #ddd;"><strong>Default Key Fallback Status</strong></td>
                                    <td style="padding:8px; border:1px solid #ddd;">
                                        <span style="background-color:{status_color};
                                                     color:#ffffff;
                                                     padding:3px 10px;
                                                     border-radius:4px;
                                                     font-size:12px;">
                                            {fallback_status}
                                        </span>
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                        <br><br>
                        Thanks,
                        <br><br>
                        <img src="https://assets-publicbucket.s3-eu-west-1.amazonaws.com/AmplifiPro/Web/V1/mail/Amplifi-PRO-LogoNew.png"
                            width="80" alt="AmplifiPRO">
                        <br><br>
                        <span style="color:#00af9b;
                            font-family:Arial,Helvetica,sans-serif;
                            font-size:14px;
                            text-decoration:none;">
                            {WEBSITE_DISPLAY_TEXT}
                        </span>
                </td>
            </tr>
            <tr><td colspan="3">&nbsp;</td></tr>
            <tr>
                <td align="left" colspan="3">
                    <a href="https://linkedin.com/company/amplifipro" target="_blank"
                       style="text-decoration:none;">
                        <img width="22" height="22" style="width:22px; height:22px;"
                             src="https://dhtfm98jq31zo.cloudfront.net/AmplifiPro/Web/V1/mail/in.png"
                             alt="LinkedIn">
                    </a>
                    &nbsp;&nbsp;
                    <a href="https://twitter.com/amplifipro" target="_blank"
                       style="text-decoration:none;">
                        <img width="22" height="22" style="width:22px; height:22px;"
                             src="https://dhtfm98jq31zo.cloudfront.net/AmplifiPro/Web/V1/mail/twitterX1.png"
                             alt="X (Twitter)">
                    </a>
                    &nbsp;&nbsp;
                    <a href="https://instagram.com/amplifipro" target="_blank"
                       style="text-decoration:none;">
                        <img width="22" height="22" style="width:22px; height:22px;"
                             src="https://dhtfm98jq31zo.cloudfront.net/AmplifiPro/Web/V1/mail/instagram1.png"
                             alt="Instagram">
                    </a>
                    &nbsp;&nbsp;
                    <a href="https://youtube.com/@amplifipro" target="_blank"
                       style="text-decoration:none;">
                        <img width="22" height="22" style="width:22px; height:22px;"
                             src="https://dhtfm98jq31zo.cloudfront.net/AmplifiPro/Web/V1/mail/youtube1.png"
                             alt="YouTube">
                    </a>
                    <br><br>
                    <span style="margin:0 auto; border-top:2px solid #9abac4; display:block;"></span>
                </td>
            </tr>
            <tr>
                <td style="margin:0; padding:0;
                           font-family:Arial,Corbel,Helvetica,sans-serif;
                           color:#666; font-size:10px; padding-right:10px;"
                    colspan="3">
                    <p style="text-decoration:none; font-weight:bold; color:#002035;
                               font-size:14px; padding-top:10px; padding-bottom:5px;
                               font-family:Arial,Corbel,sans-serif;">
                        Disclaimer
                    </p>
                    This is a system-generated email; please do not reply.<br>
                    This e-mail may contain confidential and/or legally privileged information.
                    If you are not the intended recipient (or have received this message in error)
                    please notify the sender immediately and delete this e-mail. Any unauthorised
                    copying, disclosure, distribution, or any action taken or omitted to be taken
                    in reliance on the material in this email is strictly forbidden and may be unlawful.
                </td>
            </tr>
            <tr><td colspan="3">&nbsp;</td></tr>
            <tr><td colspan="3">&nbsp;</td></tr>
        </tbody>
    </table>
    """


def _render_failure_email_html(template_kwargs):
    return _build_email_html(**_kwargs_for_callable(_build_email_html, template_kwargs))


def send_ai_failure_mail(
    error_msg,
    client_id=None,
    project_id_openai=None,
    key_tracking_id_openai=None,
    request_id=None,
):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        url = env("MAIL_API_URL")
        mail_key = env("MAIL_API_KEY")

        headers = {
            "Mailkey": mail_key,
            "User-Agent": "PythonRequests/2.31.0",
        }

        subject = "Amplifi Pro | AI Forecast OpenAI failure notification"
        now_date = datetime.now().strftime("%Y-%m-%d")
        now_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        template_kwargs = {
            "client_id": client_id or "N/A",
            "client_name": getattr(settings, "AI_FORECAST_CLIENT_NAME", "AI Forecast"),
            "intelligence_type_id": str(
                getattr(settings, "AI_FORECAST_INTELLIGENCE_TYPE_ID", "100001")
            ),
            "project_id_openai": project_id_openai or "N/A",
            "key_tracking_id_openai": key_tracking_id_openai or "N/A",
            "failure_reason": error_msg,
            "request_id": request_id or "N/A",
            "environment": getattr(settings, "MODE", "N/A"),
            "fallback_status": "Failed",
            "header_date": now_date,
            "timestamp": now_stamp,
        }

        body = _render_failure_email_html(template_kwargs)

        payload = {
            "FromMail": getattr(settings, "AI_FORECAST_FROM_EMAIL", "amplifipro@thesmartcube.com"),
            "ToMail": getattr(settings, "AI_FORECAST_FAILURE_EMAIL", "mayank.gupta@wns.com"),
            "BCC": "",
            "Provider": "sendinblue",
            "Subject": subject,
            "MailMessage": body,
        }

        ai_forecast_logger.info("[AI_MAIL] Sending Amplifi Pro failure mail")
        response = requests.post(
            url=url,
            headers=headers,
            data=payload,
            verify=bool(getattr(settings, "AI_FORECAST_MAIL_VERIFY_SSL", False)),
            timeout=30,
        )
        ai_forecast_logger.info("[AI_MAIL] Status: %s", response.status_code)
        return response

    except Exception as exc:
        ai_forecast_logger.exception("[AI_MAIL_FAILED] %s", exc)
        return None
