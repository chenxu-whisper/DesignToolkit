import { useId } from 'react'
import { FieldError } from './FieldError'

type SizeInputProps = {
  disabled?: boolean
  error?: string
  id?: string
  label: string
  onChange: (value: string) => void
  placeholder?: string
  required?: boolean
  value: string
}

export function SizeInput({
  disabled = false,
  error,
  id,
  label,
  onChange,
  placeholder = 'WIDTHxHEIGHT',
  required = false,
  value,
}: SizeInputProps) {
  const generatedId = useId()
  const inputId = id ?? generatedId
  const errorId = `${inputId}-error`

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
      <input
        aria-describedby={error ? errorId : undefined}
        aria-invalid={error ? 'true' : undefined}
        className="text-input"
        disabled={disabled}
        id={inputId}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        required={required}
        type="text"
        value={value}
      />
      <FieldError id={errorId} message={error} />
    </div>
  )
}
