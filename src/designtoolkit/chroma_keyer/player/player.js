(function (global) {
  'use strict'

  const vertexSource = `
    attribute vec2 position;
    varying vec2 uv;
    void main() {
      uv = (position + 1.0) * 0.5;
      gl_Position = vec4(position, 0.0, 1.0);
    }`
  const fragmentSource = `
    precision highp float;
    uniform sampler2D frame;
    uniform vec2 originalSize;
    uniform vec2 storedSize;
    varying vec2 uv;
    void main() {
      // Top-left crop, at texel centers. Clamp each crop before adding the half offset.
      vec2 pixel = clamp(vec2(uv.x, 1.0 - uv.y) * originalSize,
                         vec2(0.5), originalSize - vec2(0.5));
      vec2 colorUV = pixel / vec2(storedSize.x * 2.0, storedSize.y);
      vec2 alphaUV = (pixel + vec2(storedSize.x, 0.0)) /
                     vec2(storedSize.x * 2.0, storedSize.y);
      vec3 color = texture2D(frame, colorUV).rgb;
      float alpha = texture2D(frame, alphaUV).r;
      gl_FragColor = vec4(color, alpha);
    }`

  function validateMetadata(value) {
    if (!value || value.version !== 1 || value.layout !== 'color_left_alpha_right' ||
        value.alpha_mode !== 'straight') {
      throw new Error('Invalid metadata: expected version 1, color_left_alpha_right, straight alpha.')
    }
    for (const key of ['original_width', 'original_height', 'stored_width', 'stored_height']) {
      if (!Number.isSafeInteger(value[key]) || value[key] < 1) {
        throw new Error(`Invalid metadata: ${key} must be a positive integer.`)
      }
    }
    if (value.stored_width !== Math.ceil(value.original_width / 2) * 2 ||
        value.stored_height !== Math.ceil(value.original_height / 2) * 2) {
      throw new Error('Invalid metadata dimensions: stored size must be the original size padded to even.')
    }
    return { ...value }
  }

  class PackedAlphaPlayer extends EventTarget {
    constructor(canvas) {
      super()
      if (!(canvas instanceof HTMLCanvasElement)) throw new TypeError('A canvas is required.')
      this.canvas = canvas
      this.video = document.createElement('video')
      this.video.preload = 'auto'
      this.video.playsInline = true
      this.video.muted = true
      this.status = 'empty'
      this.message = 'Select an MP4 and its matching JSON metadata.'
      this.metadata = null
      this._listeners = new AbortController()
      this._load = null
      this._url = null
      this._generation = 0
      this._frame = null
      this._frameKind = null
      this._resources = null
      this.gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: false })
      if (!this.gl) throw new Error('WebGL is unavailable. Enable hardware acceleration or use a supported browser.')
      try { this._createResources() } catch (error) {
        this._deleteResources()
        throw error
      }
      const listen = (target, name, callback) =>
        target.addEventListener(name, callback, { signal: this._listeners.signal })
      listen(this.video, 'play', () => {
        if (this._canDraw()) {
          this._state('playing', 'Playing')
          this._schedule()
        }
      })
      listen(this.video, 'pause', () => {
        this._stop()
        if (this._canDraw()) {
          this._state('paused', 'Paused')
          this.render()
        }
      })
      listen(this.video, 'ended', () => {
        this._stop()
        if (this._canDraw()) this._state('paused', 'Paused at end')
      })
      listen(this.video, 'seeked', () => {
        if (this._canDraw()) this.render()
        this.dispatchEvent(new Event('timeupdate'))
      })
      listen(this.video, 'timeupdate', () => this.dispatchEvent(new Event('timeupdate')))
      listen(this.video, 'volumechange', () => this.dispatchEvent(new Event('volumechange')))
      listen(this.video, 'error', () => {
        if (this.status !== 'loading' && this.status !== 'destroyed') {
          this._fail(new Error('Video decode failed. Select a supported H.264 MP4 and matching metadata.'))
        }
      })
      listen(canvas, 'webglcontextlost', event => {
        event.preventDefault()
        this._state('context-lost', 'WebGL context lost. Waiting for restoration; playback is stopped.')
        this._stop()
        this.video.pause()
        if (this._load) {
          this._load.abort()
          this.metadata = null
          this._releaseSource()
        }
        this._resources = null
      })
      listen(canvas, 'webglcontextrestored', () => {
        try {
          this._createResources()
          const ready = this.metadata && this.video.readyState >= 2
          this._state(ready ? 'paused' : 'empty', ready ? 'Paused (WebGL restored)' : 'WebGL restored. Select files again.')
          if (ready) this.render()
        } catch (error) { this._fail(error) }
      })
    }

    _state(status, message) {
      this.status = status
      this.message = message
      this.dispatchEvent(new CustomEvent('statechange', { detail: { status, message } }))
    }

    _canDraw() {
      return ['ready', 'playing', 'paused'].includes(this.status) &&
        this.metadata && this.video.readyState >= 2 && this._resources
    }

    _createResources() {
      const gl = this.gl
      const shaders = []
      const resources = { program: null, buffer: null, texture: null }
      this._resources = resources
      try {
        for (const [type, source] of [[gl.VERTEX_SHADER, vertexSource], [gl.FRAGMENT_SHADER, fragmentSource]]) {
          const shader = gl.createShader(type)
          if (!shader) throw new Error('WebGL shader allocation failed.')
          shaders.push(shader)
          gl.shaderSource(shader, source)
          gl.compileShader(shader)
          if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            throw new Error(`WebGL shader failed: ${gl.getShaderInfoLog(shader)}`)
          }
        }
        resources.program = gl.createProgram()
        if (!resources.program) throw new Error('WebGL program allocation failed.')
        shaders.forEach(shader => gl.attachShader(resources.program, shader))
        gl.linkProgram(resources.program)
        if (!gl.getProgramParameter(resources.program, gl.LINK_STATUS)) {
          throw new Error(`WebGL program failed: ${gl.getProgramInfoLog(resources.program)}`)
        }
        resources.buffer = gl.createBuffer()
        resources.texture = gl.createTexture()
        if (!resources.buffer || !resources.texture) throw new Error('WebGL resource allocation failed.')
        gl.bindBuffer(gl.ARRAY_BUFFER, resources.buffer)
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW)
        gl.bindTexture(gl.TEXTURE_2D, resources.texture)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
        resources.original = gl.getUniformLocation(resources.program, 'originalSize')
        resources.stored = gl.getUniformLocation(resources.program, 'storedSize')
        resources.frame = gl.getUniformLocation(resources.program, 'frame')
        resources.position = gl.getAttribLocation(resources.program, 'position')
        gl.disable(gl.BLEND)
        gl.disable(gl.DEPTH_TEST)
      } catch (error) {
        this._deleteResources()
        throw error
      } finally {
        shaders.forEach(shader => gl.deleteShader(shader))
      }
    }

    _deleteResources() {
      if (!this._resources) return
      const { texture, program, buffer } = this._resources
      if (texture) this.gl.deleteTexture(texture)
      if (program) this.gl.deleteProgram(program)
      if (buffer) this.gl.deleteBuffer(buffer)
      this._resources = null
    }

    _clear() {
      if (this.gl.isContextLost()) return
      this.gl.clearColor(0, 0, 0, 0)
      this.gl.clear(this.gl.COLOR_BUFFER_BIT)
    }

    _stop() {
      if (this._frame !== null) {
        if (this._frameKind === 'video') this.video.cancelVideoFrameCallback(this._frame)
        else cancelAnimationFrame(this._frame)
      }
      this._frame = null
    }

    _schedule() {
      if (this._frame !== null || !this._canDraw() || this.video.paused) return
      const draw = () => {
        this._frame = null
        if (this._canDraw() && !this.video.paused) {
          this.render()
          this._schedule()
        }
      }
      this._frameKind = this.video.requestVideoFrameCallback ? 'video' : 'animation'
      this._frame = this._frameKind === 'video' ?
        this.video.requestVideoFrameCallback(draw) : requestAnimationFrame(draw)
    }

    _fail(error) {
      if (this.status === 'destroyed' || this.status === 'context-lost') return
      this._state('error', error.message || String(error))
      this._stop()
      this.video.pause()
      this._clear()
      this.dispatchEvent(new CustomEvent('error', { detail: error }))
    }

    _releaseSource() {
      this.video.pause()
      this.video.removeAttribute('src')
      this.video.load()
      if (this._url) URL.revokeObjectURL(this._url)
      this._url = null
    }

    async load(source, metadata) {
      if (this.status === 'destroyed') throw new Error('Player has been destroyed.')
      const generation = ++this._generation
      this._load?.abort()
      const controller = new AbortController()
      this._load = controller
      this._state('loading', 'Loading metadata and decoding video...')
      this._stop()
      this._releaseSource()
      this.metadata = null
      this._clear()
      try {
        if (metadata instanceof Blob) {
          if (metadata.size > 1024 * 1024) throw new Error('Invalid metadata: JSON exceeds 1 MiB.')
          try { metadata = JSON.parse(await metadata.text()) } catch {
            throw new Error('Invalid metadata: select a valid JSON file.')
          }
        }
        if (controller.signal.aborted) throw new DOMException('Load cancelled', 'AbortError')
        const value = validateMetadata(metadata)
        if (this.gl.isContextLost()) throw new Error('WebGL context lost. Select files after restoration.')
        const textureLimit = this.gl.getParameter(this.gl.MAX_TEXTURE_SIZE)
        const viewportLimit = this.gl.getParameter(this.gl.MAX_VIEWPORT_DIMS)
        if (value.stored_width * 2 > textureLimit || value.stored_height > textureLimit ||
            value.original_width > viewportLimit[0] || value.original_height > viewportLimit[1]) {
          throw new Error('Metadata dimensions exceed this device WebGL limits.')
        }
        this.metadata = value
        if (!(source instanceof Blob) && (typeof source !== 'string' || !source)) {
          throw new Error('Select an MP4 file or provide a video URL.')
        }
        this.video.crossOrigin = 'anonymous'
        const url = source instanceof Blob ? (this._url = URL.createObjectURL(source)) : source
        await new Promise((resolve, reject) => {
          const video = this.video
          const finish = error => {
            clearTimeout(timer)
            video.removeEventListener('loadeddata', ready)
            video.removeEventListener('error', failed)
            controller.signal.removeEventListener('abort', aborted)
            if (error) reject(error)
            else resolve()
          }
          const ready = () => finish()
          const failed = () => finish(new Error('Video decode failed. Select a supported H.264 MP4 and matching metadata.'))
          const aborted = () => finish(new DOMException('Load cancelled', 'AbortError'))
          const timer = setTimeout(() => finish(new Error('Video metadata/decode timed out. Check the MP4 and network access.')), 15000)
          video.addEventListener('loadeddata', ready)
          video.addEventListener('error', failed)
          controller.signal.addEventListener('abort', aborted, { once: true })
          video.src = url
          video.load()
        })
        if (generation !== this._generation || controller.signal.aborted) throw new DOMException('Load cancelled', 'AbortError')
        if (this.video.videoWidth !== value.stored_width * 2 || this.video.videoHeight !== value.stored_height) {
          throw new Error('Video dimensions do not match metadata. Select the matching MP4 and JSON.')
        }
        if (!Number.isFinite(this.video.duration) || this.video.duration <= 0) {
          throw new Error('Video metadata/decode failed: no finite duration.')
        }
        this.canvas.width = value.original_width
        this.canvas.height = value.original_height
        this._state('ready', `Ready / ${value.original_width} x ${value.original_height}`)
        if (!this.render()) throw new Error(this.message)
        this.dispatchEvent(new Event('timeupdate'))
        return this
      } catch (error) {
        if (generation === this._generation && this.status !== 'destroyed' && this.status !== 'context-lost') {
          this._fail(error)
          this._releaseSource()
          this.metadata = null
        }
        throw error
      } finally {
        if (this._load === controller) this._load = null
      }
    }

    render() {
      if (!this._canDraw()) return false
      const gl = this.gl
      const resources = this._resources
      const value = this.metadata
      try {
        gl.useProgram(resources.program)
        gl.bindBuffer(gl.ARRAY_BUFFER, resources.buffer)
        gl.enableVertexAttribArray(resources.position)
        gl.vertexAttribPointer(resources.position, 2, gl.FLOAT, false, 0, 0)
        gl.activeTexture(gl.TEXTURE0)
        gl.bindTexture(gl.TEXTURE_2D, resources.texture)
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false)
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false)
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, this.video)
        const uploadError = gl.getError()
        if (uploadError !== gl.NO_ERROR) throw new Error(`WebGL upload error ${uploadError}`)
        gl.uniform1i(resources.frame, 0)
        gl.uniform2f(resources.original, value.original_width, value.original_height)
        gl.uniform2f(resources.stored, value.stored_width, value.stored_height)
        gl.viewport(0, 0, this.canvas.width, this.canvas.height)
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
        const drawError = gl.getError()
        if (drawError !== gl.NO_ERROR) throw new Error(`WebGL draw error ${drawError}`)
        return true
      } catch (error) {
        this._fail(new Error(`Video texture failed: ${error.message}. Remote videos require CORS permission.`))
        return false
      }
    }

    async play() {
      if (!this._canDraw()) throw new Error('Load a valid MP4 and metadata before playback.')
      try { await this.video.play() } catch (error) {
        const failure = new Error(`Playback rejected: ${error.message}. Press Play to retry.`)
        if (this._canDraw()) this._state('paused', failure.message)
        throw failure
      }
    }

    pause() {
      this._stop()
      this.video.pause()
    }

    seek(seconds) {
      if (!this._canDraw()) throw new Error('Load a valid video before seeking.')
      if (!Number.isFinite(seconds)) throw new TypeError('Seek time must be finite.')
      this.video.currentTime = Math.max(0, Math.min(seconds, this.video.duration))
    }

    setMuted(muted) { this.video.muted = Boolean(muted) }

    destroy() {
      if (this.status === 'destroyed') return
      ++this._generation
      this._load?.abort()
      this._listeners.abort()
      this._stop()
      this._releaseSource()
      this._clear()
      this._deleteResources()
      this.metadata = null
      this._state('destroyed', 'Player destroyed')
    }
  }

  // Classic script export works from file:// and on a host page without a bundler.
  global.PackedAlphaPlayer = PackedAlphaPlayer

  function setupDemo() {
    const demo = document.getElementById('packed-alpha-demo')
    if (!demo) return
    const find = id => demo.querySelector(`#${id}`)
    const status = find('status')
    const play = find('play')
    const mute = find('mute')
    const seek = find('seek')
    let player
    try { player = new PackedAlphaPlayer(find('preview')) } catch (error) {
      status.textContent = error.message
      status.dataset.error = 'true'
      return
    }
    global.player = player
    const updateTime = () => {
      const duration = Number.isFinite(player.video.duration) ? player.video.duration : 0
      seek.max = String(duration)
      seek.value = String(player.video.currentTime)
      find('time').textContent = `${player.video.currentTime.toFixed(2)} / ${duration.toFixed(2)} s`
    }
    const update = () => {
      status.textContent = player.message
      status.dataset.error = String(['error', 'context-lost'].includes(player.status))
      const enabled = ['ready', 'playing', 'paused'].includes(player.status)
      play.disabled = mute.disabled = seek.disabled = !enabled
      play.textContent = player.status === 'playing' ? 'Pause' : 'Play'
      mute.textContent = player.video.muted ? 'Unmute' : 'Mute'
      updateTime()
    }
    player.addEventListener('statechange', update)
    player.addEventListener('timeupdate', updateTime)
    player.addEventListener('volumechange', update)
    const loadFiles = async () => {
      const video = find('video-file').files[0]
      const metadata = find('metadata-file').files[0]
      if (!video || !metadata) return
      try { await player.load(video, metadata) } catch { /* Player owns visible error state. */ }
    }
    find('video-file').addEventListener('change', loadFiles)
    find('metadata-file').addEventListener('change', loadFiles)
    play.addEventListener('click', async () => {
      if (player.status === 'playing') player.pause()
      else {
        try { await player.play() } catch { /* Rejection is visible and retryable. */ }
      }
    })
    mute.addEventListener('click', () => player.setMuted(!player.video.muted))
    seek.addEventListener('input', () => {
      try { player.seek(Number(seek.value)) } catch (error) { status.textContent = error.message }
    })
    find('background').addEventListener('change', event => {
      find('stage').dataset.background = event.target.value
    })
    global.addEventListener('pagehide', event => {
      if (event.persisted) player.pause()
      else player.destroy()
    })
    update()
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setupDemo, { once: true })
  else setupDemo()
})(window)
