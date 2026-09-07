import { FolderOpen, Upload } from 'lucide-react'
import { type DragEvent, useEffect, useId, useRef, useState } from 'react'
import { FieldError } from './FieldError'

const DROP_PATH_ERROR = '浏览器未提供文件真实路径，请使用文件夹按钮粘贴本地路径'
const PICKER_ERROR = '无法打开系统选择器，请手动输入本地路径'

type PathInputProps = {
  disabled?: boolean
  error?: string
  id?: string
  label: string
  multiline?: boolean
  onChange: (value: string) => void
  onCommit?: (value: string) => void
  pickPaths?: () => Promise<readonly string[]>
  placeholder?: string
  required?: boolean
  resolveDroppedFilePaths?: (names: readonly string[]) => Promise<readonly string[]>
  rows?: number
  uploadAccept?: string
  uploadFiles?: (
    files: readonly File[],
    onProgress: (percentage: number) => void,
    signal?: AbortSignal,
  ) => Promise<readonly string[]>
  value: string
}

export function PathInput({
  disabled = false,
  error,
  id,
  label,
  multiline = false,
  onChange,
  onCommit,
  pickPaths,
  placeholder,
  required = false,
  resolveDroppedFilePaths,
  rows = 4,
  uploadAccept,
  uploadFiles,
  value,
}: PathInputProps) {
  const generatedId = useId()
  const inputId = id ?? generatedId
  const errorId = `${inputId}-error`
  const dropErrorId = `${inputId}-drop-error`
  const pickerErrorId = `${inputId}-picker-error`
  const editorId = `${inputId}-editor`
  const [isEditorOpen, setIsEditorOpen] = useState(false)
  const [isDragActive, setIsDragActive] = useState(false)
  const [isPicking, setIsPicking] = useState(false)
  const [draftPath, setDraftPath] = useState(value)
  const [dropError, setDropError] = useState<string | null>(null)
  const [pickerError, setPickerError] = useState<string | null>(null)
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const currentValueRef = useRef(value)
  const latestIntentRef = useRef(0)
  const pickerPointerSequenceRef = useRef(0)
  const suppressPickerBlurRef = useRef(false)
  const skipNextBlurRef = useRef(false)
  const uploadControllerRef = useRef<AbortController | null>(null)
  const uploadInputRef = useRef<HTMLInputElement>(null)
  currentValueRef.current = value

  useEffect(() => {
    if (isEditorOpen) {
      document.getElementById(editorId)?.focus()
    }
  }, [editorId, isEditorOpen])

  useEffect(() => {
    if (disabled) {
      latestIntentRef.current += 1
      uploadControllerRef.current?.abort()
      uploadControllerRef.current = null
      setIsPicking(false)
      setUploadProgress(null)
    }
  }, [disabled])

  useEffect(() => {
    return () => {
      latestIntentRef.current += 1
      uploadControllerRef.current?.abort()
      uploadControllerRef.current = null
    }
  }, [])

  const openEditor = () => {
    if (!isEditorOpen) {
      setDraftPath(currentValueRef.current)
    }
    setIsEditorOpen(true)
  }

  const beginIntent = () => {
    latestIntentRef.current += 1
    return latestIntentRef.current
  }

  const isLatestIntent = (intent: number) => latestIntentRef.current === intent

  const updateValue = (nextValue: string) => {
    currentValueRef.current = nextValue
    skipNextBlurRef.current = false
    setDropError(null)
    setPickerError(null)
    onChange(nextValue)
  }

  const commitValue = (nextValue: string) => {
    updateValue(nextValue)
    onCommit?.(nextValue)
  }

  const commitManualValue = (nextValue: string) => {
    beginIntent()
    onCommit?.(nextValue)
  }

  const applyDraftPath = () => {
    if (disabled) {
      return
    }
    beginIntent()
    commitValue(draftPath)
    setIsEditorOpen(false)
  }

  const clearPickerPointerBlur = () => {
    suppressPickerBlurRef.current = false
  }

  const armPickerPointerBlur = () => {
    const pointerSequence = pickerPointerSequenceRef.current + 1
    pickerPointerSequenceRef.current = pointerSequence
    suppressPickerBlurRef.current = true
    window.setTimeout(() => {
      if (pickerPointerSequenceRef.current === pointerSequence) {
        clearPickerPointerBlur()
      }
    }, 0)
  }

  const handlePickerClick = async () => {
    if (disabled || isPicking) {
      return
    }
    if (uploadFiles) {
      uploadInputRef.current?.click()
      return
    }
    const intent = beginIntent()
    if (!pickPaths) {
      openEditor()
      return
    }

    setDropError(null)
    setPickerError(null)
    setIsPicking(true)
    try {
      const paths = await pickPaths()
      if (!isLatestIntent(intent)) {
        return
      }
      if (paths.length > 0) {
        commitValue(valueWithDroppedPaths(currentValueRef.current, paths, multiline))
        setIsEditorOpen(false)
      }
    } catch {
      if (isLatestIntent(intent)) {
        setPickerError(PICKER_ERROR)
        openEditor()
      }
    } finally {
      setIsPicking(false)
    }
  }

  const handleUploads = async (files: readonly File[]) => {
    if (disabled || isPicking || !uploadFiles || files.length === 0) {
      return
    }
    const intent = beginIntent()
    const controller = new AbortController()
    uploadControllerRef.current?.abort()
    uploadControllerRef.current = controller
    setPickerError(null)
    setDropError(null)
    setIsPicking(true)
    setUploadProgress(0)
    try {
      const references = await uploadFiles(
        files,
        (percentage) => {
          if (isLatestIntent(intent) && !controller.signal.aborted) {
            setUploadProgress(percentage)
          }
        },
        controller.signal,
      )
      if (isLatestIntent(intent) && references.length > 0) {
        commitValue(
          valueWithDroppedPaths(currentValueRef.current, references, multiline),
        )
      }
    } catch (error) {
      if (isLatestIntent(intent)) {
        setPickerError(
          error instanceof Error && error.message
            ? error.message
            : '文件上传失败',
        )
      }
    } finally {
      if (uploadControllerRef.current === controller) {
        uploadControllerRef.current = null
      }
      if (isLatestIntent(intent)) {
        setIsPicking(false)
        setUploadProgress(null)
        if (uploadInputRef.current) {
          uploadInputRef.current.value = ''
        }
      }
    }
  }

  const handleDragOver = (event: DragEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    if (disabled) {
      return
    }
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
    setIsDragActive(true)
  }

  const handleDrop = async (event: DragEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    if (disabled) {
      return
    }
    event.preventDefault()
    const intent = beginIntent()
    setIsDragActive(false)
    if (uploadFiles && event.dataTransfer.files.length > 0) {
      await handleUploads(Array.from(event.dataTransfer.files))
      return
    }
    const fileNames = droppedFileNames(event.dataTransfer.files)
    const paths = droppedPathsFromDataTransfer(event.dataTransfer)
    if (paths.length === 0) {
      if (fileNames.length > 0 && resolveDroppedFilePaths) {
        const resolvedPaths = await resolveDroppedFilePaths(fileNames)
        if (!isLatestIntent(intent)) {
          return
        }
        if (resolvedPaths.length > 0) {
          commitValue(valueWithDroppedPaths(currentValueRef.current, resolvedPaths, multiline))
          return
        }
      }
      if (fileNames.length > 0) {
        setDropError(DROP_PATH_ERROR)
      }
      return
    }
    commitValue(valueWithDroppedPaths(currentValueRef.current, paths, multiline))
  }

  const inputClassName = `text-input${isDragActive ? ' path-drop-active' : ''}`
  const textareaClassName = `${inputClassName} path-textarea`
  const describedBy = [
    error ? errorId : undefined,
    dropError ? dropErrorId : undefined,
    pickerError ? pickerErrorId : undefined,
  ]
    .filter(Boolean)
    .join(' ')
  const ariaDescribedBy = describedBy || undefined
  const isPickerDisabled = disabled || isPicking
  const visibleValue = uploadFiles ? uploadedFileNames(value) : value

  return (
    <div className="field">
      <div className="field-label-row">
        <label className="field-label" htmlFor={inputId}>
          {label}
        </label>
        {required ? (
          <span aria-hidden="true" className="field-required">
            *
          </span>
        ) : null}
      </div>
      <div className="path-input-row">
        {multiline ? (
          <textarea
            aria-describedby={ariaDescribedBy}
            aria-invalid={error ? 'true' : undefined}
            className={textareaClassName}
            disabled={disabled}
            id={inputId}
            onChange={(event) => {
              beginIntent()
              updateValue(event.target.value)
            }}
            onDragLeave={() => setIsDragActive(false)}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
            placeholder={placeholder}
            required={required}
            rows={rows}
            readOnly={Boolean(uploadFiles)}
            value={visibleValue}
          />
        ) : (
          <input
            aria-describedby={ariaDescribedBy}
            aria-invalid={error ? 'true' : undefined}
            className={inputClassName}
            disabled={disabled}
            id={inputId}
            onBlur={(event) => {
              if (skipNextBlurRef.current) {
                skipNextBlurRef.current = false
                return
              }
              if (suppressPickerBlurRef.current) {
                clearPickerPointerBlur()
                return
              }
              commitManualValue(event.currentTarget.value)
            }}
            onChange={(event) => {
              beginIntent()
              updateValue(event.target.value)
            }}
            onDragLeave={() => setIsDragActive(false)}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                event.preventDefault()
                skipNextBlurRef.current = true
                commitManualValue(event.currentTarget.value)
              }
            }}
            placeholder={placeholder}
            required={required}
            readOnly={Boolean(uploadFiles)}
            type="text"
            value={visibleValue}
          />
        )}
        {uploadFiles ? (
          <input
            accept={uploadAccept}
            disabled={disabled}
            hidden
            multiple={multiline}
            onChange={(event) => {
              void handleUploads(Array.from(event.currentTarget.files ?? []))
            }}
            ref={uploadInputRef}
            type="file"
          />
        ) : null}
        <button
          aria-controls={isEditorOpen ? editorId : undefined}
          aria-disabled={isPickerDisabled}
          aria-expanded={isEditorOpen}
          aria-label={`${uploadFiles ? '上传' : '指定'}${label}`}
          className={`icon-button path-picker-button${isEditorOpen || isPicking ? ' path-picker-button-active' : ''}`}
          onClick={() => {
            clearPickerPointerBlur()
            void handlePickerClick()
          }}
          onPointerDown={(event) => {
            if (event.button === 0) {
              armPickerPointerBlur()
            }
          }}
          title={`${uploadFiles ? '上传' : '指定'}${label}`}
          type="button"
        >
          {uploadFiles ? (
            <Upload aria-hidden="true" size={18} strokeWidth={2} />
          ) : (
            <FolderOpen aria-hidden="true" size={18} strokeWidth={2} />
          )}
        </button>
      </div>
      {uploadProgress !== null ? (
        <progress
          aria-label="上传进度"
          className="upload-progress"
          max={100}
          value={uploadProgress}
        />
      ) : null}
      {isEditorOpen ? (
        <div aria-label={`指定${label}`} className="path-editor" role="dialog">
          <label className="field-label" htmlFor={editorId}>
            本地路径
          </label>
          {multiline ? (
            <textarea
              className="text-input path-textarea"
              disabled={disabled}
              id={editorId}
              onChange={(event) => setDraftPath(event.target.value)}
              rows={rows}
              value={draftPath}
            />
          ) : (
            <input
              className="text-input"
              disabled={disabled}
              id={editorId}
              onChange={(event) => setDraftPath(event.target.value)}
              type="text"
              value={draftPath}
            />
          )}
          <div className="path-editor-actions">
            <button
              className="secondary-action-button"
              disabled={disabled}
              onClick={applyDraftPath}
              type="button"
            >
              应用路径
            </button>
            <button className="secondary-action-button" onClick={() => setIsEditorOpen(false)} type="button">
              取消
            </button>
          </div>
        </div>
      ) : null}
      <FieldError id={dropErrorId} message={dropError ?? undefined} />
      <FieldError id={pickerErrorId} message={pickerError ?? undefined} />
      <FieldError id={errorId} message={error} />
    </div>
  )
}

function droppedPathsFromDataTransfer(dataTransfer: DataTransfer): string[] {
  return uniquePaths([
    ...pathsFromText(dataTransfer.getData('text/uri-list')),
    ...pathsFromText(dataTransfer.getData('text/plain')),
    ...pathsFromFiles(dataTransfer.files),
  ])
}

function pathsFromText(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith('#'))
    .map((line) => fileUriToWindowsPath(line) ?? line)
}

function fileUriToWindowsPath(value: string): string | null {
  if (!value.toLowerCase().startsWith('file:')) {
    return null
  }
  try {
    const url = new URL(value)
    if (url.protocol !== 'file:') {
      return null
    }
    let pathname = decodeURIComponent(url.pathname)
    if (/^\/[A-Za-z]:/.test(pathname)) {
      pathname = pathname.slice(1)
    }
    const normalizedPath = pathname.replace(/\//g, '\\')
    return url.hostname ? `\\\\${url.hostname}${normalizedPath}` : normalizedPath
  } catch {
    return null
  }
}

function pathsFromFiles(files: FileList): string[] {
  return Array.from(files)
    .map((file) => (file as File & { path?: string }).path)
    .filter((path): path is string => Boolean(path))
}

function droppedFileNames(files: FileList): string[] {
  return Array.from(files)
    .map((file) => file.name.trim())
    .filter((name) => name.length > 0)
}

function valueWithDroppedPaths(
  currentValue: string,
  droppedPaths: readonly string[],
  multiline: boolean,
): string {
  if (!multiline) {
    return droppedPaths[0] ?? currentValue
  }
  return uniquePaths([...pathLines(currentValue), ...pathLines(droppedPaths.join('\n'))]).join('\n')
}

function pathLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

function uniquePaths(paths: string[]): string[] {
  return Array.from(new Set(paths))
}

function uploadedFileNames(value: string): string {
  return pathLines(value)
    .map((reference) => {
      const parts = reference.split(':', 3)
      if (parts[0] !== 'asset' || !parts[2]) {
        return reference
      }
      try {
        return decodeURIComponent(parts[2])
      } catch {
        return parts[2]
      }
    })
    .join('\n')
}
