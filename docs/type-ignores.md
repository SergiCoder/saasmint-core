# Accepted `type: ignore` / `noqa` suppressions

These suppressions are intentional and stem from upstream library limitations or deliberate design choices. Don't remove them blindly.

## django-stubs / drf-stubs

- `# type: ignore[type-arg]` on `admin.ModelAdmin`, `BaseUserAdmin`, `forms.ModelForm` — generic in stubs but **not subscriptable at runtime**. Django autodiscovers admin modules at import time, so `ModelAdmin[Model]` causes `TypeError`.
- `# type: ignore[misc]` on `permission_classes`, `throttle_classes`, `parser_classes` — DRF stubs type these as instance vars; using `ClassVar` (required by RUF012) triggers mypy `misc`.
- `# type: ignore[misc]` on `super().get_queryset()` in admin — django-stubs returns `QuerySet[Any]`; narrowing to `QuerySet[Model]` triggers `misc`.
- `# type: ignore[no-untyped-call]` on drf-spectacular `OpenApiAuthenticationExtension` — missing stubs.

## Stripe stubs

- `# type: ignore[no-untyped-call]` — `Webhook.construct_event`, `SignatureVerificationError` missing return annotations.
- `# type: ignore[arg-type]` — stub overloads don't match actual API signatures (`locale`, `**params`).
- `# type: ignore[return-value]` — `session.url` typed as `str | None` but always `str` for hosted checkout.

## Celery

- `# type: ignore[untyped-decorator]` on `@app.task` — celery has no type stubs.
- `# type: ignore[attr-defined]` on `self.retry` / `self.request` in bound tasks — injected by Celery at runtime.

## pydantic-settings

- `# type: ignore[call-arg]` on `_Env()` — fields read from env vars; mypy sees no positional args.

## Ruff / design-correct

- `# noqa: DJ001` — nullable `CharField`/`TextField` where `NULL` has semantic meaning (e.g. no avatar vs empty string).
- `# noqa: RUF012` — `Meta.constraints` / `Meta.indexes` must be mutable lists; `ClassVar` doesn't apply.
- `# noqa: ANN401` — `*args`/`**kwargs` forwarded to parent methods; `Any` is appropriate.
- `# noqa: F403` / `F405` / `E402` — star imports in settings files; standard Django inheritance pattern.
- `# noqa: S106` / `S107` — hardcoded passwords in test fixtures.
- `# noqa: F401` — side-effect import to register drf-spectacular auth extension.

## Test-only

- `# type: ignore[misc]` on frozen dataclass field mutation — testing that frozen models raise on mutation.
