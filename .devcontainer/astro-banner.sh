# Sourced at the end of dev.bashrc: show the astro dock banner once per terminal.
# Commands run by agents, ssh, scripts or BASH_ENV never pay for it: each check
# below is a shell builtin, and Python starts only for a person at a terminal.
# ASTRO_BANNER=0 opts out; ASTRO_BANNER_SHOWN keeps nested shells and exec bash quiet.

astro_person_shell() {
  [[ $- == *i* && -t 0 && -t 1 && -z ${BASH_EXECUTION_STRING-} ]] || return 1
  # ssh forced commands, and agents or editors driving a shell.
  [[ -z ${SSH_ORIGINAL_COMMAND-}${CLAUDECODE-}${CLAUDE_CODE_ENTRYPOINT-}${CODEX_SANDBOX-}${CODEX_MANAGED_BY_NPM-} ]] || return 1
  [[ -z ${CURSOR_AGENT-}${GEMINI_CLI-}${AIDER_MODEL-}${OPENCODE-}${VSCODE_RESOLVING_ENVIRONMENT-} ]] || return 1
}

astro_banner_wanted() {
  astro_person_shell || return 1
  [[ ${TERM:-dumb} != dumb && ${ASTRO_BANNER:-1} != 0 && -z ${ASTRO_BANNER_SHOWN-} ]] || return 1
}

if astro_banner_wanted; then
  export ASTRO_BANNER_SHOWN=1
  python3 -I "${BASH_SOURCE[0]%/*}/astro_banner.py" 2>/dev/null
fi
# A clone pulled without its profiles submodule (dev.bashrc's check): one line in every person's shell.
if astro_person_shell && declare -F __astro_profiles_missing >/dev/null && __astro_profiles_missing; then
  printf '\033[1;33mWARNING:\033[0m %s\n' "$__ASTRO_PROFILES_HINT" >&2
fi
unset -f astro_banner_wanted astro_person_shell
