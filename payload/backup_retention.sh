#!/usr/bin/env bash

tg115_validate_backup_dir() {
  local backup_dir="${1:-}" logical physical
  [[ "$backup_dir" == /* && -d "$backup_dir" && ! -L "$backup_dir" ]] || return 1
  logical="$(cd "$backup_dir" && pwd -L)" || return 1
  physical="$(cd "$backup_dir" && pwd -P)" || return 1
  [[ "$logical" == "$physical" && "$logical" == "$backup_dir" ]]
}

tg115_backup_pattern_stats() {
  local backup_dir="$1" pattern="$2"
  find "$backup_dir" -maxdepth 1 -type f -name "$pattern" -printf '%s\n' \
    | awk '{count += 1; bytes += $1} END {printf "%d %d\n", count, bytes}'
}

tg115_backup_inventory() {
  local backup_dir="${1:-/opt/tg115-backups}"
  local config_count config_bytes database_count database_bytes env_count env_bytes
  tg115_validate_backup_dir "$backup_dir" || {
    printf 'TG115_BACKUPS=FAILED\n备份目录不是安全的真实绝对目录：%s\n' "$backup_dir" >&2
    return 2
  }
  read -r config_count config_bytes < <(
    tg115_backup_pattern_stats "$backup_dir" 'config-*.tar.gz'
  )
  read -r database_count database_bytes < <(
    tg115_backup_pattern_stats "$backup_dir" 'database-*.db'
  )
  read -r env_count env_bytes < <(
    tg115_backup_pattern_stats "$backup_dir" 'env-*.env'
  )
  printf 'TG115_BACKUPS=OK\n'
  printf 'BACKUP_DIR=%s\n' "$backup_dir"
  printf 'CONFIG_COUNT=%s\nCONFIG_BYTES=%s\n' "$config_count" "$config_bytes"
  printf 'DATABASE_COUNT=%s\nDATABASE_BYTES=%s\n' "$database_count" "$database_bytes"
  printf 'ENV_COUNT=%s\nENV_BYTES=%s\n' "$env_count" "$env_bytes"
  printf 'TOTAL_COUNT=%s\nTOTAL_BYTES=%s\n' \
    "$((config_count + database_count + env_count))" \
    "$((config_bytes + database_bytes + env_bytes))"
}

tg115_prune_backup_pattern() {
  local backup_dir="$1" pattern="$2" keep="$3"
  local seen=0 deleted=0 entry path base
  while IFS= read -r -d '' entry; do
    ((seen += 1))
    ((seen > keep)) || continue
    path="${entry#* }"
    [[ "$(dirname -- "$path")" == "$backup_dir" ]] || return 1
    base="$(basename -- "$path")"
    case "$pattern" in
      'config-*.tar.gz') [[ "$base" == config-*.tar.gz ]] || return 1 ;;
      'database-*.db') [[ "$base" == database-*.db ]] || return 1 ;;
      'env-*.env') [[ "$base" == env-*.env ]] || return 1 ;;
      *) return 1 ;;
    esac
    [[ -f "$path" && ! -L "$path" ]] || continue
    rm -f -- "$path" || return 1
    ((deleted += 1))
  done < <(
    find "$backup_dir" -maxdepth 1 -type f -name "$pattern" \
      -printf '%T@ %p\0' | LC_ALL=C sort -z -nr
  )
  printf '%s\n' "$deleted"
}

tg115_prune_backups() {
  local backup_dir="${1:-/opt/tg115-backups}" keep="${2:-5}"
  local config_deleted database_deleted env_deleted
  [[ "$keep" =~ ^[1-9][0-9]?$ && "$keep" -le 50 ]] || {
    printf 'TG115_BACKUP_PRUNE=FAILED\n保留数量必须是 1 到 50 的整数\n' >&2
    return 2
  }
  tg115_validate_backup_dir "$backup_dir" || {
    printf 'TG115_BACKUP_PRUNE=FAILED\n备份目录不是安全的真实绝对目录：%s\n' "$backup_dir" >&2
    return 2
  }
  config_deleted="$(tg115_prune_backup_pattern "$backup_dir" 'config-*.tar.gz' "$keep")" || return 1
  database_deleted="$(tg115_prune_backup_pattern "$backup_dir" 'database-*.db' "$keep")" || return 1
  env_deleted="$(tg115_prune_backup_pattern "$backup_dir" 'env-*.env' "$keep")" || return 1
  printf 'TG115_BACKUP_PRUNE=OK\n'
  printf 'KEEP_PER_TYPE=%s\n' "$keep"
  printf 'DELETED_CONFIG=%s\nDELETED_DATABASE=%s\nDELETED_ENV=%s\n' \
    "$config_deleted" "$database_deleted" "$env_deleted"
  printf 'DELETED_TOTAL=%s\n' \
    "$((config_deleted + database_deleted + env_deleted))"
}
