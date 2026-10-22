"""
One place that turns "this account's chosen AI provider" into a text completion.

The whole app used to call Anthropic directly with the server key. This keeps
that as the default, but lets a user bring their own key (identity.LLMCredential)
for any of five providers. Anthropic goes through the `anthropic` SDK (the proven
streamed / adaptive-thinking pattern); OpenAI, Gemini, DeepSeek and any custom
OpenAI-compatible endpoint all go through the one `openai` SDK with a different
base_url + model — so five providers, two code paths.

Callers pass an `LLMConfig` (from `resolve_config(owner)`) or None to mean
"use the server's Anthropic key".

The server's key is not everyone's to spend. An account someone made for
themselves in the app (see `auth_views`) has to bring its own: anyone can make
one, so paying for theirs would be an open tab. `resolve_config` is where that
is decided, which is why every caller goes through it.
"""

import logging
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

# The server-key default model; also the Anthropic provider default. Matches the
# model the direct calls used before this module existed.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# Per-provider defaults. `base_url` blank means the SDK default (OpenAI proper).
# `key_url` is where a user gets a key — surfaced by the app, not used here.
PROVIDERS = {
    "anthropic": {
        "label": "Claude (Anthropic)", "base_url": "",
        "default_model": DEFAULT_ANTHROPIC_MODEL,
        "key_url": "https://console.anthropic.com/settings/keys",
    },
    "openai": {
        "label": "OpenAI", "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "key_url": "https://aistudio.google.com/app/apikey",
    },
    "deepseek": {
        "label": "DeepSeek", "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)", "base_url": "",
        "default_model": "", "key_url": "",
    },
}


# Accounts made through sign-up are put in this group, and membership is what
# withholds the server's key. A group rather than a column so it needs no
# migration, and so she can lift it for one person from the admin by taking
# them out of the group.
SELF_MADE_GROUP = "Signed up in the app"


class LLMUnavailable(Exception):
    """The model couldn't be reached, or no usable provider is configured.
    Message is safe to show the user."""


@dataclass
class LLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str
    # Anthropic keys made outside any workspace must name one per request.
    workspace_id: str = ""


# What an account with no key of its own gets when the server's isn't offered
# to it: `complete` answers this with "add your own API key", without calling
# anything.
NO_KEY = LLMConfig(provider="anthropic", api_key="", model="", base_url="")


def uses_server_key(owner):
    """Whether the server's Anthropic key covers this account when it has no
    key of its own. True for the owner and partner-provisioned members; false
    for an account made through sign-up."""
    return not owner.groups.filter(name=SELF_MADE_GROUP).exists()


def resolve_config(owner):
    """The owner's active LLMCredential as an LLMConfig. Without a usable one:
    None to fall back to the server Anthropic key, or NO_KEY for an account
    that has to bring its own. Defaults model/base_url from PROVIDERS when blank."""
    if owner is None or not getattr(owner, "is_authenticated", False):
        return None
    from .models import LLMCredential

    fallback = None if uses_server_key(owner) else NO_KEY

    cred = LLMCredential.objects.filter(owner=owner, is_active=True).first()
    if cred is None:
        return fallback
    try:
        api_key = cred.get_key()
    except Exception:
        logger.warning("Could not decrypt LLM key for owner %s", getattr(owner, "pk", "?"))
        return fallback
    if not api_key:
        return fallback

    registry = PROVIDERS.get(cred.provider, {})
    return LLMConfig(
        provider=cred.provider,
        api_key=api_key,
        model=cred.model or registry.get("default_model", ""),
        base_url=cred.base_url or registry.get("base_url", ""),
        workspace_id=cred.workspace_id,
    )


def complete(system, user, *, config=None, max_tokens=4096):
    """Return the model's text for a system + single user message. Raises
    LLMUnavailable with a user-facing message on any failure."""
    if config is None or config.provider == "anthropic":
        api_key = config.api_key if config else settings.ANTHROPIC_API_KEY
        workspace_id = config.workspace_id if config else settings.ANTHROPIC_WORKSPACE_ID
        model = (config.model if config and config.model else DEFAULT_ANTHROPIC_MODEL)
        if not api_key:
            raise LLMUnavailable(
                "No AI provider is configured. Add your own API key in "
                "Identity → AI provider."
            )
        return _complete_anthropic(system, user, api_key=api_key, model=model, max_tokens=max_tokens,
                                   workspace_id=workspace_id)

    if not config.api_key:
        raise LLMUnavailable("No API key set for this provider.")
    if not config.model:
        raise LLMUnavailable("No model set for this provider — pick one in Identity → AI provider.")
    return _complete_openai(system, user, config=config)


# What Anthropic says when a key made outside any workspace is used without
# naming one. Matched to turn a raw 400 into something a person can act on.
_NO_WORKSPACE = "not scoped to a workspace"

NO_WORKSPACE_MESSAGE = (
    "This Claude API key isn't tied to a workspace, so Anthropic needs to know "
    "which one to use. Either create a new key inside a workspace in the Claude "
    "Console, or add your workspace ID (it starts with wrkspc_) next to this key "
    "in Identity → AI provider."
)


def _complete_anthropic(system, user, *, api_key, model, max_tokens, workspace_id=""):
    import anthropic

    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    client = anthropic.Anthropic(api_key=api_key, default_headers=headers)
    try:
        # Streamed with adaptive thinking: a long response with a big thinking
        # budget runs past the SDK's non-streaming ceiling and is exactly what
        # trips request timeouts. get_final_message() still returns one message.
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            response = stream.get_final_message()
    except Exception as error:
        logger.exception("Anthropic call failed")
        if _NO_WORKSPACE in str(error):
            raise LLMUnavailable(NO_WORKSPACE_MESSAGE) from error
        raise LLMUnavailable(f"Couldn't reach the model: {error}") from error
    return "".join(block.text for block in response.content if block.type == "text")


def _complete_openai(system, user, *, config):
    """OpenAI, DeepSeek, Gemini (compat endpoint) and custom all land here.

    max_tokens is deliberately not sent: providers/models disagree on the field
    name (max_tokens vs max_completion_tokens) and on caps, and the prompts
    already bound the output length. Letting the provider default avoids a whole
    class of 400s.
    """
    from openai import OpenAI

    kwargs = {"api_key": config.api_key, "timeout": 300}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    client = OpenAI(**kwargs)
    try:
        completion = client.chat.completions.create(
            model=config.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as error:
        logger.exception("OpenAI-compatible call failed (provider=%s)", config.provider)
        raise LLMUnavailable(f"Couldn't reach the model: {error}") from error
    return completion.choices[0].message.content or ""
