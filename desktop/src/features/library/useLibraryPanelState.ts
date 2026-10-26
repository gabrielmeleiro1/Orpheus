import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { useGenerationGuard } from "../../state/generationGuard";
import type { CleaningPreviewResponse } from "../../types/api";
import type { UseLibraryResult } from "./useLibrary";

interface UseLibraryPanelStateArgs {
  library: UseLibraryResult;
}

export interface UseLibraryPanelStateResult {
  editTitle: string;
  editAuthor: string;
  editTags: string;
  setEditTitle: (value: string) => void;
  setEditAuthor: (value: string) => void;
  setEditTags: (value: string) => void;
  /** Whether the draft differs from the selected book's stored metadata. */
  metadataDirty: boolean;
  saveMetadata: () => Promise<void>;
  cleaningPreview: CleaningPreviewResponse | null;
  cleaningPreviewLoading: boolean;
  loadCleaningPreview: (chapterNumber: number) => Promise<void>;
  closeCleaningPreview: () => void;
}

/**
 * View-local state for LibraryPanel: the editable metadata mirror and the
 * async cleaning-preview loading. Extracted so the panel is a pure view that
 * renders state and calls handlers.
 */
export function useLibraryPanelState({ library }: UseLibraryPanelStateArgs): UseLibraryPanelStateResult {
  const selected = library.selectedBookDetail;
  const selectedId = selected?.id ?? null;
  const requestCleaningPreview = library.loadCleaningPreview;

  const [editTitle, updateEditTitle] = useState("");
  const [editAuthor, updateEditAuthor] = useState("");
  const [editTags, updateEditTags] = useState("");
  const [cleaningPreview, setCleaningPreview] = useState<CleaningPreviewResponse | null>(null);
  const [cleaningPreviewLoading, setCleaningPreviewLoading] = useState(false);
  const previewGuard = useGenerationGuard();
  const selectedBookIdRef = useRef<number | null>(selected?.id ?? null);
  useLayoutEffect(() => {
    selectedBookIdRef.current = selected?.id ?? null;
  });

  const libraryGenerationRef = useRef(library.libraryGeneration);
  const metadataRef = useRef<typeof selected>(null);
  const draftRevisionRef = useRef(0);
  const syncedRevisionRef = useRef(0);
  const submittedDraftRef = useRef<{ bookId: number; revision: number } | null>(null);

  const setEditTitle = useCallback((value: string) => {
    draftRevisionRef.current += 1;
    updateEditTitle(value);
  }, []);
  const setEditAuthor = useCallback((value: string) => {
    draftRevisionRef.current += 1;
    updateEditAuthor(value);
  }, []);
  const setEditTags = useCallback((value: string) => {
    draftRevisionRef.current += 1;
    updateEditTags(value);
  }, []);

  useEffect(() => {
    const changedBook = metadataRef.current?.id !== selected?.id
      || libraryGenerationRef.current !== library.libraryGeneration;
    libraryGenerationRef.current = library.libraryGeneration;
    metadataRef.current = selected;
    const submitted = submittedDraftRef.current;
    const submittedDraftUnchanged = submitted?.bookId === selected?.id
      && submitted?.revision === draftRevisionRef.current;
    if (changedBook) draftRevisionRef.current += 1;
    if (changedBook || submittedDraftUnchanged || syncedRevisionRef.current === draftRevisionRef.current) {
      updateEditTitle(selected?.title ?? "");
      updateEditAuthor(selected?.author ?? "");
      updateEditTags(selected?.tags ?? "");
      syncedRevisionRef.current = draftRevisionRef.current;
    }
    submittedDraftRef.current = null;
    previewGuard.invalidate();
    setCleaningPreview(null);
    setCleaningPreviewLoading(false);
  }, [library.libraryGeneration, selected?.id, selected?.title, selected?.author, selected?.tags]);

  const saveMetadata = useCallback(async () => {
    if (!selected || library.savingMetadata) return;
    submittedDraftRef.current = { bookId: selected.id, revision: draftRevisionRef.current };
    await library.updateSelectedBookMetadata({ title: editTitle, author: editAuthor, tags: editTags });
  }, [selected, library, editTitle, editAuthor, editTags]);

  const loadCleaningPreview = useCallback(
    async (chapterNumber: number): Promise<void> => {
      previewGuard.invalidate();
      const ownsPreview = previewGuard.capture();
      const requestBookId = selectedId;
      const isCurrentPreview = () => ownsPreview() && selectedBookIdRef.current === requestBookId;
      setCleaningPreviewLoading(true);
      try {
        const preview = await requestCleaningPreview(chapterNumber);
        if (isCurrentPreview()) {
          setCleaningPreview(preview);
        }
      } finally {
        if (isCurrentPreview()) {
          setCleaningPreviewLoading(false);
        }
      }
    },
    [requestCleaningPreview, selectedId],
  );

  return {
    closeCleaningPreview: () => {
      previewGuard.invalidate();
      setCleaningPreview(null);
      setCleaningPreviewLoading(false);
    },
    editTitle,
    editAuthor,
    editTags,
    setEditTitle,
    setEditAuthor,
    setEditTags,
    metadataDirty: Boolean(selected) && (
      editTitle !== (selected?.title ?? "")
      || editAuthor !== (selected?.author ?? "")
      || editTags !== (selected?.tags ?? "")
    ),
    saveMetadata,
    cleaningPreview,
    cleaningPreviewLoading,
    loadCleaningPreview,
  };
}
