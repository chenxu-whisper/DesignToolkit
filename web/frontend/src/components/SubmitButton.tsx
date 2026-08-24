import type { ReactNode } from 'react'

type SubmitButtonProps = {
  children: ReactNode
  disabled?: boolean
}

export function SubmitButton({ children, disabled = false }: SubmitButtonProps) {
  return (
    <button className="submit-button" disabled={disabled} type="submit">
      {children}
    </button>
  )
}
