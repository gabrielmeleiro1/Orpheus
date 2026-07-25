# Paste and Job Metadata Design

## Goal

Remove ambiguity between metadata for a selected queue item and metadata for a new pasted source, and keep the Dashboard's active-job label accurate after frontend reload reconciliation.

## UI and queue ownership

The Paste text card will own dedicated optional Title and Author fields. These values apply only to the pending pasted document. Adding the paste will send its title to the upload endpoint, copy both values onto the new queue item, select that item, and clear the paste text and paste metadata after success. A failed upload will preserve all draft values so the user can retry.

The existing Title and Author fields below the source and queue controls will continue to edit only the selected queue item. Paste creation will no longer inspect or infer intent from those fields, so editable rows and books with identical metadata cannot affect the new paste.

## Active-job metadata contract

`ConversionJob` will carry immutable optional title and author metadata from job creation. New conversions receive the request metadata; repair jobs receive the existing library book metadata. The active-conversion API payload will expose these fields along with the existing job id, status, book id, and error.

The frontend event-stream refresh method will return the active-conversion response after applying its status effects. `useConversionJob` will use that response to restore the job title and author during mount, reconnect, and manual reconciliation. A successfully started job will continue to update the same metadata immediately without waiting for a poll.

## Error and lifecycle behavior

Paste drafts clear only after upload and queue insertion succeed. Backend or upload errors retain the text, title, and author. Job metadata is display-only and does not change conversion behavior. Missing title or author remains valid and falls back to the Dashboard's existing generic copy.

## Testing

Frontend regression tests will cover dedicated paste controls, same-title and editable-row isolation, successful draft clearing, failed-upload preservation, and metadata recovery from active-job reconciliation. Backend tests will cover conversion and repair job metadata plus active-job serialization. Existing frontend, typecheck, Python, Ruff, and diff checks remain the final gates.
