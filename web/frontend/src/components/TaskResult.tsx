import { Download } from 'lucide-react'

type TaskResultProps = {
  hosted: boolean
  value: string
}

export function TaskResult({ hosted, value }: TaskResultProps) {
  if (!hosted || !value.startsWith('/api/')) {
    return <p className="task-result">输出：{value}</p>
  }

  return (
    <p className="task-result">
      <a download href={value}>
        <Download aria-hidden="true" size={17} />
        下载结果
      </a>
    </p>
  )
}
