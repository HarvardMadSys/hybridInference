/**
 * Marks a field that holds a credential other than the administrator's own, so
 * password-manager extensions (1Password, LastPass, Bitwarden, Dashlane)
 * neither fill it nor offer to save what is typed into it. Browsers' built-in
 * managers go by `autocomplete` instead: give the field `"new-password"` (or
 * `"off"`) and its form `autoComplete="off"`.
 */
export const NOT_A_LOGIN_FIELD = {
  'data-1p-ignore': true,
  'data-lpignore': 'true',
  'data-bwignore': true,
  'data-form-type': 'other',
} as const;
