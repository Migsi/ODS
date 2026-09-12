#!/usr/bin/env bash
# Phase 5D3/5D4A exact-candidate admission, state binding, and source rollback.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() { echo "[FAIL] $*"; exit 1; }
pass() { echo "[PASS] $*"; }

for required in git jq tar; do
    command -v "$required" >/dev/null 2>&1 || fail "$required is required"
done
command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 \
    || fail "python3 or python is required"
if command -v python3 >/dev/null 2>&1 \
    && python3 -c 'import os,sys; sys.exit(0 if os.name == "nt" else 1)' \
        >/dev/null 2>&1; then
    echo "[SKIP] Assistant First source updates are not qualified on native Windows"
    exit 0
elif command -v python >/dev/null 2>&1 \
    && python -c 'import os,sys; sys.exit(0 if os.name == "nt" else 1)' \
        >/dev/null 2>&1; then
    echo "[SKIP] Assistant First source updates are not qualified on native Windows"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REMOTE="$TMP/remote.git"
SEED="$TMP/seed"
BIN_DIR="$TMP/bin"
mkdir -p "$SEED/ods/lib" "$SEED/ods/scripts" "$SEED/ods/config" \
    "$SEED/ods/extensions/services/dashboard-api" "$BIN_DIR"

git init -q --bare "$REMOTE"
git init -q "$SEED"
git -C "$SEED" checkout -q -b main
git -C "$SEED" config user.name "ODS Update Contract"
git -C "$SEED" config user.email "ods-update-contract@example.invalid"
git -C "$SEED" config core.autocrlf false

cp "$ROOT_DIR/ods-update.sh" "$SEED/ods/ods-update.sh"
cp "$ROOT_DIR/lib/python-cmd.sh" "$SEED/ods/lib/python-cmd.sh"
cp "$ROOT_DIR/scripts/run-with-extension-mutation-guard.py" \
    "$SEED/ods/scripts/run-with-extension-mutation-guard.py"
cp "$ROOT_DIR/extensions/services/dashboard-api/extension_operation_locks.py" \
    "$SEED/ods/extensions/services/dashboard-api/extension_operation_locks.py"
chmod +x "$SEED/ods/ods-update.sh"

cat > "$SEED/ods/lib/update-snapshots.sh" <<'SH'
snapshot_pre_update() {
    printf '%s\n' snapshot >> "${UPDATE_EVENT_LOG:?}"
    local target="${INSTALL_DIR}/data/backups/pre-update-$1"
    mkdir -p "$target"
    case "${SNAPSHOT_MUTATION:-}" in
        lockfile)
            printf '%s\n' '{"schema":"changed-after-preflight"}' \
                > "${INSTALL_DIR}/data/assistant-first/desired-state/extensions.lock.json"
            ;;
        detach)
            git -C "$INSTALL_DIR" checkout --detach -q
            ;;
        remove-candidate-repository)
            rm -rf -- "${UPDATE_CANDIDATE_REPOSITORY:?}"
            ;;
        failing-migration)
            mkdir -p "${INSTALL_DIR}/migrations"
            printf '%s\n' '#!/usr/bin/env bash' 'exit 1' \
                > "${INSTALL_DIR}/migrations/migrate-v99-contract-failure.sh"
            chmod +x "${INSTALL_DIR}/migrations/migrate-v99-contract-failure.sh"
            ;;
        failing-migration-head-drift)
            mkdir -p "${INSTALL_DIR}/migrations"
            cat > "${INSTALL_DIR}/migrations/migrate-v99-head-drift.sh" <<'EOF'
#!/usr/bin/env bash
git -C "$(dirname "$0")/.." config user.name "ODS Update Contract"
git -C "$(dirname "$0")/.." config user.email "ods-update-contract@example.invalid"
git -C "$(dirname "$0")/.." commit --allow-empty -q -m "unexpected concurrent revision"
exit 1
EOF
            chmod +x "${INSTALL_DIR}/migrations/migrate-v99-head-drift.sh"
            ;;
    esac
    printf '%s\n' "$target"
}

_restore_snapshot() {
    printf '%s\n' restore >> "${UPDATE_EVENT_LOG:?}"
    return 0
}
SH

cat > "$SEED/ods/scripts/assess-extension-update.py" <<'PY'
#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--install-dir", required=True)
parser.add_argument("--candidate-dir", required=True)
parser.add_argument("--candidate-revision", required=True)
args = parser.parse_args()

candidate = Path(args.candidate_dir)
marker = (candidate / "candidate-marker.txt").read_text(encoding="utf-8").strip()
with open(os.environ["PREFLIGHT_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"revision": args.candidate_revision, "marker": marker}) + "\n")
with open(os.environ["UPDATE_EVENT_LOG"], "a", encoding="utf-8") as handle:
    handle.write("preflight\n")

if os.environ.get("REMOVE_REMOTE_AFTER_PREFLIGHT") == "1":
    Path(os.environ["REMOTE_PATH"]).rename(os.environ["REMOTE_PATH"] + ".offline")

status = int(os.environ.get("PREFLIGHT_EXIT", "0"))
if status == 12:
    result = {
        "schema": "ods.extensions.update-preflight-error.v1",
        "error": {"code": "fixture-invalid", "details": {}},
    }
else:
    result = {
        "schema": "ods.extensions.update-preflight.v1",
        "candidateSourceRevision": args.candidate_revision,
        "preflightHash": "a" * 64,
        "assessmentEnvelope": {
            "assessment": {
                "canUpdate": status != 11,
                "requiresExtensionPlan": status == 10,
            }
        },
    }
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
raise SystemExit(status)
PY
chmod +x "$SEED/ods/scripts/assess-extension-update.py"

cat > "$SEED/ods/manifest.json" <<'EOF'
{"ods_version":"1.0.0","release":{"version":"1.0.0"}}
EOF
cat > "$SEED/ods/config/extensions-catalog.json" <<'EOF'
{"catalog_revision":"base","extensions":[]}
EOF
cat > "$SEED/ods/docker-compose.base.yml" <<'EOF'
services:
  dashboard-api:
    image: example/dashboard-api:test
EOF
printf '%s\n' base > "$SEED/ods/candidate-marker.txt"

git -C "$SEED" add ods
git -C "$SEED" commit -q -m "base"
BASE_REVISION=$(git -C "$SEED" rev-parse HEAD)
git -C "$SEED" remote add origin "$REMOTE"
git -C "$SEED" push -q -u origin main
git -C "$SEED" push -q origin "${BASE_REVISION}:refs/heads/public-beta"
git -C "$REMOTE" symbolic-ref HEAD refs/heads/main

# Clone each fixture before the candidate exists so a rejected preflight can
# prove the installed object database was not changed by candidate resolution.
for name in blocked-10 blocked-11 blocked-12 blocked-42 beta legacy ready \
    dirty-staged dirty-unstaged dirty-untracked drift-lockfile drift-branch \
    apply-fail migration-fail migration-head-drift; do
    mkdir -p "$TMP/$name"
    git clone -q "$REMOTE" "$TMP/$name/repository"
done
git -C "$TMP/beta/repository" checkout -q -b public-beta --track origin/public-beta

cat > "$SEED/ods/manifest.json" <<'EOF'
{"ods_version":"1.1.0","release":{"version":"1.1.0"}}
EOF
cat > "$SEED/ods/config/extensions-catalog.json" <<'EOF'
{"catalog_revision":"candidate","extensions":[]}
EOF
printf '%s\n' candidate > "$SEED/ods/candidate-marker.txt"
git -C "$SEED" add ods
git -C "$SEED" commit -q -m "candidate"
CANDIDATE_REVISION=$(git -C "$SEED" rev-parse HEAD)
git -C "$SEED" push -q origin main

git -C "$SEED" checkout -q -b public-beta "$BASE_REVISION"
cat > "$SEED/ods/manifest.json" <<'EOF'
{"ods_version":"1.0.5","release":{"version":"1.0.5"}}
EOF
cat > "$SEED/ods/config/extensions-catalog.json" <<'EOF'
{"catalog_revision":"public-beta-candidate","extensions":[]}
EOF
printf '%s\n' public-beta-candidate > "$SEED/ods/candidate-marker.txt"
git -C "$SEED" add ods
git -C "$SEED" commit -q -m "public beta candidate"
BETA_REVISION=$(git -C "$SEED" rev-parse HEAD)
git -C "$SEED" push -q origin public-beta
git -C "$SEED" checkout -q main

cat > "$BIN_DIR/docker" <<'SH'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "${UPDATE_EVENT_LOG:?}"
if [[ "${1:-}" == "info" ]]; then
    exit 0
fi
if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then
    exit 0
fi
if [[ "${1:-}" == "compose" ]]; then
    shift
fi
case " $* " in
    *" ps --services "*)
        printf '%s\n' dashboard-api
        ;;
    *" ps --format json "*)
        printf '%s\n' '{"State":"running"}'
        ;;
esac
exit 0
SH
chmod +x "$BIN_DIR/docker"

cat > "$BIN_DIR/curl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$BIN_DIR/curl"

prepare_runtime_state() {
    local name="$1" profile="$2"
    local install="$TMP/$name/repository/ods"
    mkdir -p \
        "$install/data/assistant-first/desired-state" \
        "$install/data/backups" \
        "$TMP/$name/runtime-tmp"
    chmod go-w "$install/data"
    cat > "$install/.env" <<EOF
ODS_INSTALL_PROFILE=$profile
ODS_MODE=local
GPU_BACKEND=cpu
GPU_COUNT=1
TIER=1
DASHBOARD_API_PORT=3002
OLLAMA_PORT=8080
EOF
    printf '%s\n' '{"version":"1.0.0"}' > "$install/.version"
    printf '%s\n' \
        '{"schema":"ods.extensions.lockfile-envelope.v1","fixture":true}' \
        > "$install/data/assistant-first/desired-state/extensions.lock.json"
    printf '%s\n' '-f docker-compose.base.yml' > "$install/.compose-flags"
}

run_update() {
    local name="$1" status="$2"
    local install="$TMP/$name/repository/ods"
    local output="$TMP/$name/update.out"
    set +e
    HOME="$TMP/$name/home" \
    TMPDIR="$TMP/$name/runtime-tmp" \
    PATH="$BIN_DIR:$PATH" \
    PREFLIGHT_EXIT="$status" \
    PREFLIGHT_LOG="$TMP/$name/preflight.log" \
    UPDATE_EVENT_LOG="$TMP/$name/events.log" \
    SNAPSHOT_MUTATION="${SNAPSHOT_MUTATION:-}" \
    bash "$install/ods-update.sh" update > "$output" 2>&1
    RUN_STATUS=$?
    set -e
    SNAPSHOT_MUTATION=""
}

for status in 10 11 12 42; do
    name="blocked-$status"
    prepare_runtime_state "$name" assistant-first
    install="$TMP/$name/repository/ods"
    if git -C "$install" cat-file -e "${CANDIDATE_REVISION}^{commit}" 2>/dev/null; then
        fail "fixture $name already contains the candidate object"
    fi
    run_update "$name" "$status"
    [[ "$RUN_STATUS" -ne 0 ]] || fail "preflight status $status did not block update"
    [[ "$(git -C "$install" rev-parse HEAD)" == "$BASE_REVISION" ]] \
        || fail "preflight status $status changed installed HEAD"
    if git -C "$install" cat-file -e "${CANDIDATE_REVISION}^{commit}" 2>/dev/null; then
        fail "preflight status $status imported candidate objects before approval"
    fi
    [[ -f "$TMP/$name/events.log" ]] || {
        cat "$TMP/$name/update.out"
        fail "preflight status $status did not reach the candidate gate"
    }
    [[ "$(sed -n '1p' "$TMP/$name/events.log")" == preflight ]] \
        || fail "preflight status $status did not run the candidate gate first"
    [[ "$(wc -l < "$TMP/$name/events.log" | tr -d ' ')" == 1 ]] \
        || fail "preflight status $status reached snapshot or Docker mutation"
    [[ "$(jq -r '.revision' "$TMP/$name/preflight.log")" == "$CANDIDATE_REVISION" ]] \
        || fail "preflight status $status was not bound to the exact candidate"
    [[ "$(jq -r '.marker' "$TMP/$name/preflight.log")" == candidate ]] \
        || fail "preflight status $status did not inspect the candidate tree"
    if find "$TMP/$name/runtime-tmp" -mindepth 1 -maxdepth 1 \
        -type d -name 'ods-update-candidate.*' | grep -q .; then
        fail "preflight status $status left a candidate workspace"
    fi
done
pass "all non-ready preflight states fail before installed checkout and runtime mutation"

prepare_runtime_state beta assistant-first
run_update beta 0
[[ "$RUN_STATUS" -eq 0 ]] || {
    cat "$TMP/beta/update.out"
    fail "configured public-beta upstream update failed"
}
[[ "$(git -C "$TMP/beta/repository/ods" rev-parse HEAD)" == "$BETA_REVISION" ]] \
    || fail "configured public-beta checkout crossed to the main branch"
[[ "$(jq -r '.revision' "$TMP/beta/preflight.log")" == "$BETA_REVISION" ]] \
    || fail "public-beta preflight was not bound to its configured upstream"
[[ "$(jq -r '.marker' "$TMP/beta/preflight.log")" == public-beta-candidate ]] \
    || fail "public-beta update inspected the wrong candidate subtree"
pass "configured public-beta checkouts remain on their exact upstream channel"

prepare_runtime_state legacy full
run_update legacy 42
[[ "$RUN_STATUS" -eq 0 ]] || {
    cat "$TMP/legacy/update.out"
    fail "legacy profile update failed"
}
[[ "$(git -C "$TMP/legacy/repository/ods" rev-parse HEAD)" == \
    "$CANDIDATE_REVISION" ]] || fail "legacy update path did not retain git pull behavior"
[[ ! -e "$TMP/legacy/preflight.log" ]] \
    || fail "legacy profile entered the Assistant First preflight"
[[ "$(sed -n '1p' "$TMP/legacy/events.log")" == snapshot ]] \
    || fail "legacy profile no longer snapshots before its established pull path"
pass "Full/Core/Custom source update behavior remains on the legacy path"

prepare_runtime_state dirty-staged assistant-first
dirty_staged_install="$TMP/dirty-staged/repository/ods"
printf '%s\n' staged-change > "$dirty_staged_install/candidate-marker.txt"
git -C "$TMP/dirty-staged/repository" add ods/candidate-marker.txt
run_update dirty-staged 0
[[ "$RUN_STATUS" -ne 0 ]] || fail "staged tracked source change did not block update"
grep -q 'require a clean tracked checkout' "$TMP/dirty-staged/update.out" \
    || fail "staged tracked source rejection was not actionable"
[[ ! -e "$TMP/dirty-staged/preflight.log" ]] \
    || fail "staged tracked source rejection ran candidate preflight"
[[ "$(git -C "$dirty_staged_install" rev-parse HEAD)" == "$BASE_REVISION" ]] \
    || fail "staged tracked source rejection changed HEAD"
pass "staged tracked source changes fail before candidate fetch"

prepare_runtime_state dirty-unstaged assistant-first
dirty_unstaged_install="$TMP/dirty-unstaged/repository/ods"
printf '%s\n' unstaged-change > "$dirty_unstaged_install/candidate-marker.txt"
run_update dirty-unstaged 0
[[ "$RUN_STATUS" -ne 0 ]] || fail "unstaged tracked source change did not block update"
grep -q 'require a clean tracked checkout' "$TMP/dirty-unstaged/update.out" \
    || fail "unstaged tracked source rejection was not actionable"
[[ ! -e "$TMP/dirty-unstaged/preflight.log" ]] \
    || fail "unstaged tracked source rejection ran candidate preflight"
[[ "$(git -C "$dirty_unstaged_install" rev-parse HEAD)" == "$BASE_REVISION" ]] \
    || fail "unstaged tracked source rejection changed HEAD"
pass "unstaged tracked source changes fail before candidate fetch"

prepare_runtime_state dirty-untracked assistant-first
dirty_untracked_install="$TMP/dirty-untracked/repository/ods"
printf '%s\n' runtime-data > "$dirty_untracked_install/data/runtime-contract.txt"
run_update dirty-untracked 0
[[ "$RUN_STATUS" -eq 0 ]] || {
    cat "$TMP/dirty-untracked/update.out"
    fail "untracked runtime data blocked the source update"
}
[[ "$(git -C "$dirty_untracked_install" rev-parse HEAD)" == \
    "$CANDIDATE_REVISION" ]] || fail "untracked-data update did not advance HEAD"
pass "untracked runtime data does not fail the tracked-source precondition"

prepare_runtime_state drift-lockfile assistant-first
drift_lockfile_install="$TMP/drift-lockfile/repository/ods"
SNAPSHOT_MUTATION=lockfile run_update drift-lockfile 0
[[ "$RUN_STATUS" -ne 0 ]] || fail "post-preflight lockfile drift did not block update"
grep -q 'desired state changed after update preflight' \
    "$TMP/drift-lockfile/update.out" \
    || fail "post-preflight lockfile drift was not identified"
[[ "$(git -C "$drift_lockfile_install" rev-parse HEAD)" == "$BASE_REVISION" ]] \
    || fail "post-preflight lockfile drift changed HEAD"
if git -C "$drift_lockfile_install" cat-file -e \
    "${CANDIDATE_REVISION}^{commit}" 2>/dev/null; then
    fail "post-preflight lockfile drift imported the candidate object"
fi
! grep -q '^docker ' "$TMP/drift-lockfile/events.log" \
    || fail "post-preflight lockfile drift restarted services"
! grep -q '^restore$' "$TMP/drift-lockfile/events.log" \
    || fail "post-preflight lockfile drift restored an unmutated snapshot"
pass "lockfile drift after preflight fails before candidate import or runtime mutation"

prepare_runtime_state drift-branch assistant-first
drift_branch_install="$TMP/drift-branch/repository/ods"
SNAPSHOT_MUTATION=detach run_update drift-branch 0
[[ "$RUN_STATUS" -ne 0 ]] || fail "post-preflight branch drift did not block update"
grep -q 'source checkout changed after update preflight' \
    "$TMP/drift-branch/update.out" \
    || fail "post-preflight branch drift was not identified"
[[ "$(git -C "$drift_branch_install" rev-parse HEAD)" == "$BASE_REVISION" ]] \
    || fail "post-preflight branch drift changed the source commit"
if git -C "$drift_branch_install" cat-file -e \
    "${CANDIDATE_REVISION}^{commit}" 2>/dev/null; then
    fail "post-preflight branch drift imported the candidate object"
fi
! grep -q '^docker ' "$TMP/drift-branch/events.log" \
    || fail "post-preflight branch drift restarted services"
pass "branch drift after preflight fails before candidate import or runtime mutation"

prepare_runtime_state apply-fail assistant-first
apply_fail_install="$TMP/apply-fail/repository/ods"
SNAPSHOT_MUTATION=remove-candidate-repository run_update apply-fail 0
[[ "$RUN_STATUS" -ne 0 ]] || fail "missing assessed candidate repository did not fail"
grep -q 'failed before changing installed source' "$TMP/apply-fail/update.out" \
    || fail "pre-merge apply failure did not report its no-mutation boundary"
[[ "$(git -C "$apply_fail_install" rev-parse HEAD)" == "$BASE_REVISION" ]] \
    || fail "pre-merge apply failure changed HEAD"
! grep -q '^restore$' "$TMP/apply-fail/events.log" \
    || fail "pre-merge apply failure restored an unmutated snapshot"
! grep -q '^docker ' "$TMP/apply-fail/events.log" \
    || fail "pre-merge apply failure restarted services"
pass "apply failure before HEAD movement does not cycle the runtime"

prepare_runtime_state migration-fail assistant-first
migration_fail_install="$TMP/migration-fail/repository/ods"
SNAPSHOT_MUTATION=failing-migration run_update migration-fail 0
[[ "$RUN_STATUS" -ne 0 ]] || fail "injected migration failure did not fail update"
grep -q 'Migration failed: migrate-v99-contract-failure.sh' \
    "$TMP/migration-fail/update.out" \
    || fail "injected migration failure reason was not reported"
grep -q "Restored source revision ${BASE_REVISION}" \
    "$TMP/migration-fail/update.out" \
    || fail "migration failure did not report exact source restoration"
[[ "$(git -C "$migration_fail_install" rev-parse HEAD)" == "$BASE_REVISION" ]] \
    || fail "migration failure did not restore the original source revision"
[[ "$(sed -n '1p' "$TMP/migration-fail/events.log")" == preflight ]] \
    || fail "migration failure did not preflight first"
[[ "$(sed -n '2p' "$TMP/migration-fail/events.log")" == snapshot ]] \
    || fail "migration failure did not snapshot second"
[[ "$(sed -n '3p' "$TMP/migration-fail/events.log")" == restore ]] \
    || fail "migration failure did not restore snapshot after source rollback"
grep -q '^docker .* down --remove-orphans$' \
    "$TMP/migration-fail/events.log" \
    || fail "migration failure did not restart the restored graph"
pass "migration failure restores exact source before snapshot and runtime recovery"

prepare_runtime_state migration-head-drift assistant-first
migration_drift_install="$TMP/migration-head-drift/repository/ods"
SNAPSHOT_MUTATION=failing-migration-head-drift run_update migration-head-drift 0
[[ "$RUN_STATUS" -ne 0 ]] || fail "migration HEAD drift did not fail update"
migration_drift_head=$(git -C "$migration_drift_install" rev-parse HEAD)
[[ "$migration_drift_head" != "$BASE_REVISION" \
    && "$migration_drift_head" != "$CANDIDATE_REVISION" ]] \
    || fail "rollback erased or failed to create the unexpected source revision"
grep -q 'HEAD no longer matches the applied update candidate' \
    "$TMP/migration-head-drift/update.out" \
    || fail "unexpected rollback HEAD did not produce a fail-closed error"
grep -q 'manual recovery is required' "$TMP/migration-head-drift/update.out" \
    || fail "unexpected rollback HEAD did not require manual recovery"
! grep -q '^restore$' "$TMP/migration-head-drift/events.log" \
    || fail "unexpected rollback HEAD restored runtime state onto unknown source"
! grep -q '^docker ' "$TMP/migration-head-drift/events.log" \
    || fail "unexpected rollback HEAD restarted services"
pass "source rollback refuses to erase an unrecognized concurrent revision"

prepare_runtime_state ready assistant-first
ready_install="$TMP/ready/repository/ods"
set +e
HOME="$TMP/ready/home" \
TMPDIR="$TMP/ready/runtime-tmp" \
PATH="$BIN_DIR:$PATH" \
PREFLIGHT_EXIT=0 \
PREFLIGHT_LOG="$TMP/ready/preflight.log" \
UPDATE_EVENT_LOG="$TMP/ready/events.log" \
REMOVE_REMOTE_AFTER_PREFLIGHT=1 \
REMOTE_PATH="$REMOTE" \
bash "$ready_install/ods-update.sh" update > "$TMP/ready/update.out" 2>&1
ready_status=$?
set -e
[[ "$ready_status" -eq 0 ]] || {
    cat "$TMP/ready/update.out"
    fail "ready exact-candidate update failed after the network disappeared"
}
[[ "$(git -C "$ready_install" rev-parse HEAD)" == "$CANDIDATE_REVISION" ]] \
    || fail "ready update did not apply the preflighted exact candidate"
ready_first=$(sed -n '1p' "$TMP/ready/events.log" | tr -d '\r')
ready_second=$(sed -n '2p' "$TMP/ready/events.log" | tr -d '\r')
[[ "$ready_first" == preflight && "$ready_second" == snapshot ]] \
    || fail "ready update did not complete preflight before snapshot"
grep -q '^docker .* down --remove-orphans$' "$TMP/ready/events.log" \
    || fail "ready update did not reach runtime restart after exact checkout"
[[ -d "${REMOTE}.offline" ]] \
    || fail "network disappearance fixture did not run after preflight"
if find "$TMP/ready/runtime-tmp" -mindepth 1 -maxdepth 1 \
    -type d -name 'ods-update-candidate.*' | grep -q .; then
    fail "successful update left a candidate workspace"
fi
pass "ready update applies only the preflighted object without a second network fetch"

echo "[PASS] Phase 5D3/5D4A Assistant First source update contract"
