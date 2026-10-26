import { useCallback, useRef, useState } from "react";

import { isLibraryReplacedError, type ApiClient } from "../../api/client";
import { clearPlaybackCheckpoint, playbackCheckpointBookIdentity } from "../player/playbackCheckpoint";
import type { LibraryRepository } from "./libraryRepository";
import type { useLibraryQueries } from "./useLibraryQueries";

export function useLibraryMutations(apiClient: ApiClient | null, queries: ReturnType<typeof useLibraryQueries>) {
  const {
    repository,
    items,
    setItems,
    indexedCount,
    setIndexedCount,
    selectedBookId,
    setSelectedBookId,
    selectedBookIndex,
    setSelectedBookIndex,
    selectedBookDetail,
    setSelectedBookDetail,
    setLoadingList,
    setLoadingMore,
    setLoadingDetail,
    setErrorMessage,
    setStatusMessage,
    pendingDeletionPath,
    setPendingDeletionPath,
    queryContextGenerationRef,
    reloadContextIdentityRef,
    beginListRequest,
    beginDetailRequest,
    beginMutationRequest,
    beginPreviewRequest,
    beginEntityActionRequest,
    beginApiOwnedRequest,
    listAbortRef,
    loadMoreAbortRef,
    loadMoreInFlightRef,
    listFenceRef,
    detailAbortRef,
    selectedBookIdRef,
    inFlightDeleteTokensRef,
    selectionGenerationRef,
    preservedStatusRef,
    beginSelectionOwnedRequest,
    loadLibrary,
    reloadLibrarySession,
  } = queries;

  const selectedEpoch = selectedBookDetail?.id === selectedBookId
    ? selectedBookDetail.library_epoch
    : items.find((item) => item.id === selectedBookId)?.library_epoch;
  const metadataSavesRef = useRef(new Map<number, { repository: LibraryRepository; token: symbol }>());
  const [metadataSaves, setMetadataSaves] = useState(metadataSavesRef.current);
  const openPendingDeletionFolder = useCallback(async () => {
    if (!apiClient || !pendingDeletionPath) return;
    try {
      await apiClient.openDeletionFolder(pendingDeletionPath);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Could not open folder");
    }
  }, [apiClient, pendingDeletionPath]);

  /** Fire-and-report action against the selected book: identical selection
   * ownership, status, and error handling for every entity endpoint. */
  const runEntityAction = useCallback(
    async <T,>(
      call: (repo: LibraryRepository, bookId: number) => Promise<T>,
      describeSuccess: (result: T) => string,
      fallbackMessage: string,
    ): Promise<void> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("No book selected");
        return;
      }

      const isCurrentLibrary = beginApiOwnedRequest(() => () => true);
      setPendingDeletionPath(null);
      const targetBookId = selectedBookId;
      const isCurrentAction = beginSelectionOwnedRequest(
        beginEntityActionRequest,
        targetBookId,
      );
      setErrorMessage("");

      try {
        const result = await call(repository, targetBookId);
        if (!isCurrentAction()) {
          return;
        }
        setStatusMessage(describeSuccess(result));
      } catch (error: unknown) {
        if (isCurrentLibrary() && isLibraryReplacedError(error)) {
          await reloadLibrarySession();
          return;
        }
        if (!isCurrentAction()) {
          return;
        }
        const message = error instanceof Error ? error.message : fallbackMessage;
        setErrorMessage(message);
        setStatusMessage(message);
      }
    },
    [beginApiOwnedRequest, beginEntityActionRequest, beginSelectionOwnedRequest, reloadLibrarySession, repository, selectedBookId],
  );

  const openSelectedBookFolder = useCallback(
    (): Promise<void> =>
      runEntityAction(
        (repo, bookId) => repo.openBookFolder(bookId),
        (response) =>
          response.folder_path?.trim() ? `opened folder: ${response.folder_path}` : "opened folder",
        "failed to open book folder",
      ),
    [runEntityAction],
  );

  const deleteSelectedBook = useCallback(async (releaseBookAudio?: (bookId: number) => void): Promise<boolean> => {
    if (!repository || !selectedBookId) {
      setStatusMessage("No book selected");
      return false;
    }

    const isCurrentLibrary = beginApiOwnedRequest(() => () => true);
    const deleteOwnerToken = Symbol();
    if (inFlightDeleteTokensRef.current.has(selectedBookId)) {
      return false;
    }
    setPendingDeletionPath(null);
    const deleteController = new AbortController();
    inFlightDeleteTokensRef.current.set(selectedBookId, {
      token: deleteOwnerToken,
      controller: deleteController,
    });

    const title = selectedBookDetail?.title || items.find((item) => item.id === selectedBookId)?.title || "selected book";
    let deletedStatus = `Deleted ${title}`;
    const selectionGenerationAtDeleteStart = selectionGenerationRef.current;
    const isDeleteQueryCurrent = queryContextGenerationRef.capture();
    const isCurrentMutation = beginApiOwnedRequest(beginMutationRequest);
    const isCurrentList = beginApiOwnedRequest(beginListRequest);
    listFenceRef.current = isCurrentList;
    loadMoreAbortRef.current?.abort();
    loadMoreAbortRef.current = null;
    loadMoreInFlightRef.current = false;
    setLoadingMore(false);
    listAbortRef.current?.abort();
    listAbortRef.current = null;
    detailAbortRef.current?.abort();
    detailAbortRef.current = null;
    beginDetailRequest();
    setLoadingList(false);
    setLoadingDetail(false);
    const previousItems = items;
    const previousSelectedBookId = selectedBookId;
    const previousSelectedBookIndex = selectedBookIndex;
    const previousSelectedBookDetail = selectedBookDetail;
    const previousIndexedCount = indexedCount;
    const deleteIndex = items.findIndex((item) => item.id === selectedBookId);
    const optimisticItems = items.filter((item) => item.id !== selectedBookId);
    const nextIndex = optimisticItems.length > 0 ? Math.min(Math.max(deleteIndex, 0), optimisticItems.length - 1) : -1;
    const nextBook = nextIndex >= 0 ? optimisticItems[nextIndex] : null;
    const optimisticSelectedBookId = nextBook?.id ?? null;
    if (optimisticSelectedBookId !== selectedBookIdRef.current) {
      selectionGenerationRef.invalidate();
      beginPreviewRequest();
    }
    const optimisticSelectionGeneration =
      selectionGenerationAtDeleteStart + (optimisticSelectedBookId === selectedBookId ? 0 : 1);

    setErrorMessage("");
    preservedStatusRef.current = null;
    setStatusMessage(`Deleting ${title}…`);
    setItems(optimisticItems);
    setIndexedCount(Math.max(0, indexedCount - 1));
    setSelectedBookIndex(nextIndex);
    selectedBookIdRef.current = optimisticSelectedBookId;
    setSelectedBookId(optimisticSelectedBookId);
    setSelectedBookDetail(null);

    try {
      releaseBookAudio?.(selectedBookId);
      const deletion = await repository.deleteBook(selectedBookId, { signal: deleteController.signal, expectedEpoch: selectedEpoch });
      if (!isCurrentMutation()) {
        return true;
      }
      clearPlaybackCheckpoint(selectedBookId, undefined, previousSelectedBookDetail
        ? playbackCheckpointBookIdentity(previousSelectedBookDetail) : undefined);
      if (deletion.files_pending) {
        deletedStatus = "Removed from library, but some files couldn't be deleted";
      }

      const selectionStillDeleteOwned = () =>
        selectedBookIdRef.current === optimisticSelectedBookId &&
        selectionGenerationRef.current === optimisticSelectionGeneration;
      const currentListContext = reloadContextIdentityRef.current;
      const selectionOwnedBeforeReconcile = selectionStillDeleteOwned();
      const preserveNewerSelectionMessages = !selectionOwnedBeforeReconcile;
      const publishDeletedStatus =
        selectionOwnedBeforeReconcile && isDeleteQueryCurrent();
      if (publishDeletedStatus) {
        setPendingDeletionPath(deletion.files_pending ? deletion.path ?? null : null);
        preservedStatusRef.current = deletedStatus;
        setStatusMessage(deletedStatus);
      }
      await loadLibrary(currentListContext.query, {
        modeFilter: currentListContext.modeFilter,
        silentDetailStatus:
          publishDeletedStatus || preserveNewerSelectionMessages,
        preserveMessages: preserveNewerSelectionMessages,
      });
      if (!isCurrentMutation()) {
        return true;
      }

      if (selectionStillDeleteOwned() && isDeleteQueryCurrent()) {
        setStatusMessage(deletedStatus);
        // Exactly one preserved status ("deleted X") survives the delete's own
        // silent reload; release it here so the next search-triggered detail
        // load resumes the normal "selected: X" status flow.
        preservedStatusRef.current = null;
      }
      return true;
    } catch (error: unknown) {
      if (isCurrentLibrary() && isLibraryReplacedError(error)) {
        await reloadLibrarySession();
        return false;
      }
      if (!isCurrentMutation() || !isCurrentList()) {
        return false;
      }

      const selectionStillDeleteOwned =
        selectedBookIdRef.current === optimisticSelectedBookId &&
        selectionGenerationRef.current === optimisticSelectionGeneration;
      if (!selectionStillDeleteOwned || !isDeleteQueryCurrent()) {
        const currentListContext = reloadContextIdentityRef.current;
        const preserveNewerSelectionMessages = !selectionStillDeleteOwned;
        await loadLibrary(currentListContext.query, {
          modeFilter: currentListContext.modeFilter,
          silentDetailStatus: preserveNewerSelectionMessages,
          preserveMessages: preserveNewerSelectionMessages,
        });
        return false;
      }

      const message = error instanceof Error ? error.message : "failed to delete book";
      preservedStatusRef.current = null;
      if (previousSelectedBookId !== selectedBookIdRef.current) {
        selectionGenerationRef.invalidate();
        beginPreviewRequest();
      }
      setItems(previousItems);
      setIndexedCount(previousIndexedCount);
      setSelectedBookIndex(previousSelectedBookIndex);
      selectedBookIdRef.current = previousSelectedBookId;
      setSelectedBookId(previousSelectedBookId);
      setSelectedBookDetail(previousSelectedBookDetail);
      setErrorMessage(message);
      setStatusMessage(message);
      return false;
    } finally {
      if (inFlightDeleteTokensRef.current.get(selectedBookId)?.token === deleteOwnerToken) {
        inFlightDeleteTokensRef.current.delete(selectedBookId);
      }
    }
  }, [beginApiOwnedRequest, beginDetailRequest, beginListRequest, beginMutationRequest, beginPreviewRequest, indexedCount, items, loadLibrary, reloadLibrarySession, repository, selectedBookDetail, selectedBookId, selectedBookIndex, selectedEpoch]);

  const updateSelectedBookMetadata = useCallback(
    async (payload: { title: string; author: string; tags: string }): Promise<void> => {
      if (!repository || !selectedBookId) {
        setStatusMessage("No book selected");
        return;
      }

      const isCurrentLibrary = beginApiOwnedRequest(() => () => true);
      const targetBookId = selectedBookId;
      if (metadataSavesRef.current.get(targetBookId)?.repository === repository) return;
      const saveToken = Symbol();
      metadataSavesRef.current.set(targetBookId, { repository, token: saveToken });
      setMetadataSaves(new Map(metadataSavesRef.current));
      setPendingDeletionPath(null);
      const isCurrentMutation = beginSelectionOwnedRequest(
        beginMutationRequest,
        targetBookId,
      );
      setErrorMessage("");

      try {
        const updated = await repository.updateBook(targetBookId, {
          expected_epoch: selectedEpoch,
          title: payload.title.trim() || null,
          author: payload.author.trim() || null,
          tags: payload.tags.trim(),
        });
        if (!isCurrentMutation()) {
          return;
        }
        setSelectedBookDetail((current) => ({
          ...(current ?? updated),
          ...updated,
          chapters: current?.chapters ?? updated.chapters ?? [],
          playback_url: current?.playback_url ?? updated.playback_url ?? null,
        }));
        setItems((current) =>
          current.map((item) =>
            item.id === targetBookId
              ? {
                  ...item,
                  title: updated.title,
                  author: updated.author,
                  tags: updated.tags,
                  source_available: updated.source_available,
                }
              : item,
          ),
        );
        setStatusMessage("Metadata saved");
      } catch (error: unknown) {
        if (isCurrentLibrary() && isLibraryReplacedError(error)) {
          await reloadLibrarySession();
          return;
        }
        if (!isCurrentMutation()) {
          return;
        }
        const message = error instanceof Error ? error.message : "failed to update book";
        setErrorMessage(message);
        setStatusMessage(message);
      } finally {
        if (metadataSavesRef.current.get(targetBookId)?.token === saveToken) {
          metadataSavesRef.current.delete(targetBookId);
          setMetadataSaves(new Map(metadataSavesRef.current));
        }
      }
    },
    [beginApiOwnedRequest, beginMutationRequest, beginSelectionOwnedRequest, reloadLibrarySession, repository, selectedBookId, selectedEpoch],
  );

  const repairSelectedBook = useCallback(
    (releaseBookAudio?: (bookId: number) => void): Promise<void> =>
      runEntityAction(
        (repo, bookId) => {
          releaseBookAudio?.(bookId);
          return repo.repairBook(bookId, { expectedEpoch: selectedEpoch });
        },
        // Name the book, not the job id.
        () => `Repair started for ${selectedBookDetail?.id === selectedBookId && selectedBookDetail.title ? selectedBookDetail.title : "this book"}.`,
        "failed to start repair",
      ),
    [runEntityAction, selectedBookDetail, selectedBookId, selectedEpoch],
  );

  const setSelectedPlaybackPosition = useCallback((positionSec: number) => {
    const normalizedPosition = Number(positionSec);
    if (!Number.isFinite(normalizedPosition) || normalizedPosition < 0) {
      return;
    }

    setSelectedBookDetail((current) => {
      if (!current) {
        return current;
      }
      return {
        ...current,
        playback_position_sec: normalizedPosition,
      };
    });
  }, []);

  return {
    metadataSaves,
    openPendingDeletionFolder,
    openSelectedBookFolder,
    deleteSelectedBook,
    updateSelectedBookMetadata,
    repairSelectedBook,
    setSelectedPlaybackPosition,
  };
}
