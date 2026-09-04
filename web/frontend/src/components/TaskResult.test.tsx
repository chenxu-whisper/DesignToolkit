import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TaskResult } from './TaskResult'

describe('TaskResult', () => {
  it('renders hosted task results as downloads', () => {
    render(
      <TaskResult
        hosted
        value="/api/tasks/task-1/download"
      />,
    )

    expect(screen.getByRole('link', { name: '下载结果' })).toHaveAttribute(
      'href',
      '/api/tasks/task-1/download',
    )
  })
})
