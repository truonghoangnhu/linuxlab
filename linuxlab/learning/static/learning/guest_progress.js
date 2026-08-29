/*
 * Browser-local anonymous completion cache (Task 08, ADR-006, ADR-012).
 *
 * Nothing in this file ever reaches the server on its own -- there is no
 * background sync for guests. The claim flow (claimSelectedProgress) is the
 * ONLY path that sends any of this data to the server, and it only ever
 * sends the entries the caller explicitly selects, never the whole store.
 */

const GUEST_PROGRESS_KEY = "linuxlab.guest.progress.v1";
const CLAIM_PROGRESS_URL = "/api/v1/learning/claim-progress/";

function readGuestProgress() {
    try {
        const raw = window.localStorage.getItem(GUEST_PROGRESS_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
        return [];
    }
}

function writeGuestProgress(entries) {
    window.localStorage.setItem(GUEST_PROGRESS_KEY, JSON.stringify(entries));
}

function recordLocalCompletion(activityPublicId, activityVersion) {
    const entries = readGuestProgress();
    const alreadyRecorded = entries.some(
        (entry) =>
            entry.activity_public_id === activityPublicId &&
            entry.activity_version === activityVersion
    );
    if (!alreadyRecorded) {
        entries.push({
            activity_public_id: activityPublicId,
            activity_version: activityVersion,
            completed_at: new Date().toISOString(),
        });
        writeGuestProgress(entries);
    }
    return entries;
}

/*
 * Builds a claim request body from ONLY the explicitly selected entries --
 * never the whole local store. `selected` is an array of
 * {activity_public_id, activity_version} the learner has explicitly chosen
 * to claim (e.g. via checkboxes in a "claim your progress" UI).
 */
function buildClaimPayload(selected) {
    return {
        claims: selected.map((item) => ({
            activity_public_id: item.activity_public_id,
            activity_version: item.activity_version,
        })),
    };
}

function getCsrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
}

/*
 * Sends only the explicitly selected entries to the authenticated claim
 * endpoint. The caller is responsible for deciding which locally-cached
 * entries to select; this function never reads the whole store to decide
 * what to send.
 */
async function claimSelectedProgress(selected) {
    const response = await fetch(CLAIM_PROGRESS_URL, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": getCsrfToken(),
        },
        body: JSON.stringify(buildClaimPayload(selected)),
    });
    return response.json();
}

window.LinuxLabGuestProgress = {
    GUEST_PROGRESS_KEY,
    readGuestProgress,
    recordLocalCompletion,
    buildClaimPayload,
    claimSelectedProgress,
};
