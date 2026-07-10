import { useCallback, useEffect, useState } from "react";

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

  useEffect(() => {
    setEditTitle(selected?.title ?? "");
    setEditAuthor(selected?.author ?? "");
    setEditTags(selected?.tags ?? "");
    setCleaningPreview(null);
  }, [selected?.id, selected?.title, selected?.author, selected?.tags]);

  const loadCleaningPreview = useCallback(
    async (chapterNumber: number): Promise<void> => {
      setCleaningPreviewLoading(true);
      try {
        const preview = await library.loadCleaningPreview(chapterNumber);
        setCleaningPreview(preview);
      } finally {
        setCleaningPreviewLoading(false);
      }
    },
    [library],
  );

  return {
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
