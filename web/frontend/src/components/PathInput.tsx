import { FolderOpen } from 'lucide-react'
import { type DragEvent, useEffect, useId, useState } from 'react'
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
  pickPaths?: () => Promise<readonly string[]>
  placeholder?: string
  required?: boolean
  resolveDroppedFilePaths?: (names: readonly string[]) => Promise<readonly string[]>
  rows?: number
  value: string
}

export function PathInput({
  disabled = false,
  error,
  id,
  label,
  multiline = false,
  onChange,
  pickPaths,
  placeholder,
  required = false,
  resolveDroppedFilePaths,
  rows = 4,
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

  useEffect(() => {
    if (isEditorOpen) {
      document.getElementById(editorId)?.focus()
    }
  }, [editorId, isEditorOpen])

  const openEditor = () => {
    if (!isEditorOpen) {
      setDraftPath(value)
    }
    setIsEditorOpen(true)
  }

  const applyDraftPath = () => {
    setDropError(null)
    setPickerError(null)
    onChange(draftPath)
    setIsEditorOpen(false)
  }

  const updateValue = (nextValue: string) => {
    setDropError(null)
    setPickerError(null)
    onChange(nextValue)
  }

  const handlePickerClick = async () => {
    if (disabled || isPicking) {
      return
    }
    if (!pickPaths) {
      openEditor()
      return
    }

    setDropError(null)
    setPickerError(null)
    setIsPicking(true)
    try {
      const paths = await pickPaths()
      if (paths.length > 0) {
        updateValue(valueWithDroppedPaths(value, paths, multiline))
        setIsEditorOpen(false)
      }
    } catch {
      setPickerError(PICKER_ERROR)
      openEditor()
    } finally {
      setIsPicking(false)
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
    setIsDragActive(false)
    const currentValue = event.currentTarget.value
    const fileNames = droppedFileNames(event.dataTransfer.files)
    const paths = droppedPathsFromDataTransfer(event.dataTransfer)
    if (paths.length === 0) {
      if (fileNames.length > 0 && resolveDroppedFilePaths) {
        const resolvedPaths = await resolveDroppedFilePaths(fileNames)
        if (resolvedPaths.length > 0) {
          updateValue(valueWithDroppedPaths(currentValue, resolvedPaths, multiline))
          return
        }
      }
      if (fileNames.length > 0) {
        setDropError(DROP_PATH_ERROR)
      }
      return
    }
    updateValue(valueWithDroppedPaths(currentValue, paths, multiline))
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
            onChange={(event) => updateValue(event.target.value)}
            onDragLeave={() => setIsDragActive(false)}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
            placeholder={placeholder}
            required={required}
            rows={rows}
            value={value}
          />
        ) : (
          <input
            aria-describedby={ariaDescribedBy}
            aria-invalid={error ? 'true' : undefined}
            className={inputClassName}
            disabled={disabled}
            id={inputId}
            onChange={(event) => updateValue(event.target.value)}
            onDragLeave={() => setIsDragActive(false)}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
            placeholder={placeholder}
            required={required}
            type="text"
            value={value}
          />
        )}
        <button
          aria-controls={isEditorOpen ? editorId : undefined}
          aria-expanded={isEditorOpen}
          aria-label={`指定${label}`}
          className={`icon-button path-picker-button${isEditorOpen || isPicking ? ' path-picker-button-active' : ''}`}
          disabled={disabled || isPicking}
          onClick={() => void handlePickerClick()}
          title={`指定${label}`}
          type="button"
        >
          <FolderOpen aria-hidden="true" size={18} strokeWidth={2} />
        </button>
      </div>
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
            <button className="secondary-action-button" onClick={applyDraftPath} type="button">
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
