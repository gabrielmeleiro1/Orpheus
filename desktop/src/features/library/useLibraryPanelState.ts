import { useCallback, useEffect, useRef, useState } from "react";

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

  const [editTitle, setEditTitle] = useState("");
  const [editAuthor, setEditAuthor] = useState("");
  const [editTags, setEditTags] = useState("");
  const [cleaningPreview, setCleaningPreview] = useState<CleaningPreviewResponse | null>(null);
  const [cleaningPreviewLoading, setCleaningPreviewLoading] = useState(false);
  const previewGenerationRef = useRef(0);
  const selectedBookIdRef = useRef<number | null>(selected?.id ?? null);
  selectedBookIdRef.current = selected?.id ?? null;

  useEffect(() => {
    previewGenerationRef.current += 1;
    setEditTitle(selected?.title ?? "");
    setEditAuthor(selected?.author ?? "");
    setEditTags(selected?.tags ?? "");
    setCleaningPreview(null);
    setCleaningPreviewLoading(false);
  }, [selected?.id, selected?.title, selected?.author, selected?.tags]);

  const loadCleaningPreview = useCallback(
    async (chapterNumber: number): Promise<void> => {
      const requestGeneration = ++previewGenerationRef.current;
      const requestBookId = selected?.id ?? null;
      setCleaningPreviewLoading(true);
      try {
        const preview = await library.loadCleaningPreview(chapterNumber);
        if (
          previewGenerationRef.current === requestGeneration &&
          selectedBookIdRef.current === requestBookId
        ) {
          setCleaningPreview(preview);
        }
      } finally {
        if (
          previewGenerationRef.current === requestGeneration &&
          selectedBookIdRef.current === requestBookId
        ) {
          setCleaningPreviewLoading(false);
        }
      }
    },
    [library],
  );

  return {
    closeCleaningPreview: () => {
      previewGenerationRef.current += 1;
      setCleaningPreview(null);
      setCleaningPreviewLoading(false);
    },
    editTitle,
    editAuthor,
    editTags,
    setEditTitle,
    setEditAuthor,
    setEditTags,
    cleaningPreview,
    cleaningPreviewLoading,
    loadCleaningPreview,
  };
}
