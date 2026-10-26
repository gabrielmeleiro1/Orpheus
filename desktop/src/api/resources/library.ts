import { resolveUrl, type RequestJson, type ApiRequestOptions } from "../http";
import { normalizeBookId } from "../validation";
import type {
  CleaningPreviewResponse,
  ConversionMode,
  DeleteBookResponse,
  DesktopQueryTokenRequest,
  DesktopQueryTokenResponse,
  LibraryBookDetail,
  LibraryBookUpdateRequest,
  LibraryCountResponse,
  LibraryListResponse,
  OpenBookFolderResponse,
  PlaybackPositionUpdateResponse,
} from "../../types/api";

interface LibraryQueryOptions {
  conversionMode?: ConversionMode | "";
  favorite?: boolean;
  offset?: number;
}

export interface LibraryRequestOptions extends ApiRequestOptions {
  expectedEpoch?: string;
}

/** Separate the library epoch from the transport options it must not leak into. */
function splitEpoch(options: LibraryRequestOptions | undefined): [string | undefined, ApiRequestOptions] {
  const { expectedEpoch, ...requestOptions } = options ?? {};
  return [expectedEpoch, requestOptions];
}

interface LibraryApiContext {
  requestJson: RequestJson;
  resolvedBaseUrl: string;
  usesDesktopAuth: boolean;
}

export function createLibraryApi({ requestJson, resolvedBaseUrl, usesDesktopAuth }: LibraryApiContext) {
  async function getLibrary(
    search?: string,
    limit?: number,
    filters: LibraryQueryOptions = {},
    options?: ApiRequestOptions,
  ): Promise<LibraryListResponse> {
    const params = new URLSearchParams();

    const normalizedSearch = String(search ?? "").trim();
    if (normalizedSearch) {
      params.set("search", normalizedSearch);
    }

    if (Number.isInteger(limit) && (limit ?? 0) > 0) {
      params.set("limit", String(limit));
    }

    if (filters.conversionMode) {
      params.set("conversion_mode", filters.conversionMode);
    }

    if (filters.favorite !== undefined) {
      params.set("favorite", String(filters.favorite));
    }

    if (Number.isInteger(filters.offset) && (filters.offset ?? 0) > 0) {
      params.set("offset", String(filters.offset));
    }

    const query = params.toString();
    const path = query ? `/api/library?${query}` : "/api/library";
    return requestJson<LibraryListResponse>(path, options);
  }

  async function getLibraryCount(options?: ApiRequestOptions): Promise<LibraryCountResponse> {
    return requestJson<LibraryCountResponse>("/api/library/count", options);
  }

  async function getLibraryBook(
    bookId: number,
    options?: ApiRequestOptions,
  ): Promise<LibraryBookDetail> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<LibraryBookDetail>(`/api/library/${normalizedBookId}`, options);
  }

  async function createDesktopQueryToken(
    payload: DesktopQueryTokenRequest,
    options?: ApiRequestOptions,
  ): Promise<DesktopQueryTokenResponse> {
    if (payload.scope === "audio" && !payload.expected_epoch) {
      throw new Error("expectedEpoch is required for audio query tokens");
    }
    const requestPayload: DesktopQueryTokenRequest =
      payload.scope === "audio"
        ? { scope: "audio", book_id: normalizeBookId(Number(payload.book_id)), expected_epoch: payload.expected_epoch }
        : { scope: "events" };

    return requestJson<DesktopQueryTokenResponse>("/api/desktop/query-token", {
      method: "POST",
      body: JSON.stringify(requestPayload),
      ...options,
    });
  }

  async function updateLibraryBook(
    bookId: number,
    payload: LibraryBookUpdateRequest,
    options?: ApiRequestOptions,
  ): Promise<LibraryBookDetail> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<LibraryBookDetail>(`/api/library/${normalizedBookId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
      ...options,
    });
  }

  async function getCleaningPreview(
    bookId: number,
    chapterNumber: number,
    options?: ApiRequestOptions,
  ): Promise<CleaningPreviewResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    const normalizedChapter = Math.max(1, Math.floor(Number(chapterNumber) || 0));
    return requestJson<CleaningPreviewResponse>(
      `/api/library/${normalizedBookId}/chapters/${normalizedChapter}/cleaning-preview`,
      options,
    );
  }

  async function getLibraryAudioUrl(bookId: number, options?: LibraryRequestOptions): Promise<string> {
    const normalizedBookId = normalizeBookId(bookId);
    const [expectedEpoch, requestOptions] = splitEpoch(options);
    const resolved = resolveUrl(resolvedBaseUrl, `/api/library/${normalizedBookId}/audio`);
    const url = new URL(resolved, window.location.origin);

    if (usesDesktopAuth) {
      const queryToken = await createDesktopQueryToken(
        { scope: "audio", book_id: normalizedBookId, expected_epoch: expectedEpoch },
        requestOptions,
      );
      if (queryToken.token.trim()) {
        url.searchParams.set("token", queryToken.token);
      }
    }

    return url.toString();
  }

  async function updateBookPlaybackPosition(
    bookId: number,
    positionSec: number,
    options?: LibraryRequestOptions,
  ): Promise<PlaybackPositionUpdateResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    const [expectedEpoch, requestOptions] = splitEpoch(options);
    const normalizedPosition = Number(positionSec);
    if (!Number.isFinite(normalizedPosition) || normalizedPosition < 0) {
      throw new Error("positionSec must be a non-negative number");
    }

    return requestJson<PlaybackPositionUpdateResponse>(
      `/api/library/${normalizedBookId}/playback-position`,
      {
        method: "PUT",
        body: JSON.stringify({ position_sec: normalizedPosition, expected_epoch: expectedEpoch }),
        ...requestOptions,
      },
    );
  }

  async function openBookFolder(
    bookId: number,
    options?: ApiRequestOptions,
  ): Promise<OpenBookFolderResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    return requestJson<OpenBookFolderResponse>(`/api/library/${normalizedBookId}/open-folder`, {
      method: "POST",
      ...options,
    });
  }

  async function deleteBook(bookId: number, options?: LibraryRequestOptions): Promise<DeleteBookResponse> {
    const normalizedBookId = normalizeBookId(bookId);
    const [expectedEpoch, requestOptions] = splitEpoch(options);
    const query = expectedEpoch !== undefined
      ? `?${new URLSearchParams({ expected_epoch: expectedEpoch })}`
      : "";
    return requestJson<DeleteBookResponse>(`/api/library/${normalizedBookId}` + query, {
      method: "DELETE",
      ...requestOptions,
    });
  }

  return {
    getLibrary,
    getLibraryCount,
    getLibraryBook,
    getLibraryAudioUrl,
    createDesktopQueryToken,
    updateLibraryBook,
    getCleaningPreview,
    updateBookPlaybackPosition,
    openBookFolder,
    deleteBook,
  };
}
