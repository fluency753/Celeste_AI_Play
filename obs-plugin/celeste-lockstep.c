/*
 * Celeste AI 锁步录制插件（OBS 32，Windows x64）
 *
 * 1. 锁步：录制期间，OBS 每个视频节拍（tick 回调，渲染这一帧之前）向桥接 Mod 的时钟端口发一个令牌，
 *    等桥接推进一帧并把它呈现到屏幕（最多等 WAIT_MS 毫秒），这一帧渲染时就采到了这个游戏帧。
 * 2. 只录游玩帧：插件自带一个原始画面输出（celeste_gameplay_output），只把"刚采到一个新游戏帧"的视频帧
 *    交给 ffmpeg 编码；每个保留的视频帧配上同一时段（加音频偏移）的 1/fps 秒音频，写进 WAV，结束时合成 MKV。
 *    思考期间、桥接空闲时的视频帧都不写，所以成片的帧数等于游玩帧数。
 * 3. 对账：每个保留帧记一行（成片里的序号、游戏帧号、章节计时、OBS 时间戳）到 frames.csv。
 *
 * 与桥接的协议（每行一条，见 game-bridge/Source/ClockServer.cs）：
 *   插件 → 桥接：T <id>（令牌）  L 1 / L 0（开关锁步）  S <json>（状态）
 *   桥接 → 插件：F <id> <帧号> <章节计时>（这一帧已呈现）  N <id>（没有要推进的帧）  R <json>（录制指令）
 */
#include <obs-module.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>

OBS_DECLARE_MODULE()

#define PLUGIN "celeste-lockstep"
#define CLOCK_PORT 47776
#define WAIT_MS 10
#define KEEP_RING 1024
#define AUDIO_RING_SECONDS 6

/* ------------------------------------------------------------------ 连接与锁步状态 */

static struct {
	CRITICAL_SECTION lock;
	CONDITION_VARIABLE reply_cv;
	SOCKET sock;
	volatile LONG connected;
	volatile LONG lockstep;
	volatile LONG stopping;
	HANDLE net_thread;
	/* 令牌 */
	long long next_id;
	long long outstanding;
	int reply_kind; /* 0 无, 'F', 'N' */
	long long reply_id, reply_frame, reply_ticks;
	/* 统计 */
	long long tokens, kept, late_ticks;
} g;

/* 保留帧：tick 里写入（时间戳 → 游戏帧），原始画面回调里取出 */
static struct {
	SRWLOCK lock;
	uint64_t ts[KEEP_RING];
	long long frame[KEEP_RING];
	long long ticks[KEEP_RING];
	unsigned head, count;
} keep;

static void send_line(const char *fmt, ...)
{
	char buf[4096];
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(buf, sizeof(buf) - 2, fmt, ap);
	va_end(ap);
	if (n < 0)
		return;
	if (n > (int)sizeof(buf) - 2)
		n = (int)sizeof(buf) - 2;
	buf[n++] = '\n';
	EnterCriticalSection(&g.lock);
	SOCKET s = g.sock;
	LeaveCriticalSection(&g.lock);
	if (s != INVALID_SOCKET)
		send(s, buf, n, 0);
}

static void keep_push(uint64_t ts, long long frame, long long ticks)
{
	AcquireSRWLockExclusive(&keep.lock);
	unsigned idx = (keep.head + keep.count) % KEEP_RING;
	if (keep.count == KEEP_RING) { /* 满了：丢最旧的（不应发生） */
		keep.head = (keep.head + 1) % KEEP_RING;
		keep.count--;
	}
	keep.ts[idx] = ts;
	keep.frame[idx] = frame;
	keep.ticks[idx] = ticks;
	keep.count++;
	ReleaseSRWLockExclusive(&keep.lock);
}

/* 查找并移除某个时间戳；比它更早的条目（已经错过的）一并丢弃 */
static bool keep_take(uint64_t ts, long long *frame, long long *ticks)
{
	bool found = false;
	AcquireSRWLockExclusive(&keep.lock);
	while (keep.count > 0) {
		uint64_t t = keep.ts[keep.head];
		if (t > ts)
			break;
		if (t == ts) {
			*frame = keep.frame[keep.head];
			*ticks = keep.ticks[keep.head];
			found = true;
		}
		keep.head = (keep.head + 1) % KEEP_RING;
		keep.count--;
		if (found)
			break;
	}
	ReleaseSRWLockExclusive(&keep.lock);
	return found;
}

/* OBS 视频线程：渲染这一帧之前 */
static void on_tick(void *param, float seconds)
{
	(void)param;
	(void)seconds;
	if (!g.connected || !g.lockstep)
		return;
	uint64_t ts = obs_get_video_frame_time();

	EnterCriticalSection(&g.lock);
	if (g.outstanding == 0) {
		g.outstanding = ++g.next_id;
		g.reply_kind = 0;
		g.tokens++;
		LeaveCriticalSection(&g.lock);
		send_line("T %lld", g.outstanding);
		EnterCriticalSection(&g.lock);
	}
	ULONGLONG deadline = GetTickCount64() + WAIT_MS;
	while (!(g.reply_kind && g.reply_id == g.outstanding) && g.connected) {
		ULONGLONG now = GetTickCount64();
		if (now >= deadline)
			break;
		SleepConditionVariableCS(&g.reply_cv, &g.lock, (DWORD)(deadline - now));
	}
	if (g.reply_kind && g.reply_id == g.outstanding) {
		if (g.reply_kind == 'F') {
			keep_push(ts, g.reply_frame, g.reply_ticks);
			g.kept++;
		}
		g.outstanding = 0;
		g.reply_kind = 0;
	} else {
		g.late_ticks++; /* 这一节拍没等到：下一节拍继续等同一个令牌，这一帧不保留 */
	}
	LeaveCriticalSection(&g.lock);
}

/* ------------------------------------------------------------------ 录制输出 */

struct chunk {
	uint64_t ts;
	uint32_t frames;
	float *data; /* 交错立体声 */
};

struct gp_output {
	obs_output_t *output;
	char dir[MAX_PATH];
	char ffmpeg[MAX_PATH];
	char video_args[1024];
	double audio_offset_ms;
	uint32_t width, height, fps_num, fps_den, rate;
	uint64_t frame_ns;
	HANDLE proc, stdin_wr;
	FILE *wav, *csv;
	uint64_t wav_frames;
	long long written;
	bool failed;
	char error[512];
	/* 音频 */
	SRWLOCK alock;
	struct chunk *chunks;
	size_t nchunks, cap;
	uint64_t *pending; /* 等待音频的保留帧的起始时间（已加偏移） */
	size_t npending, pcap;
};

static struct gp_output *active_output;
static obs_output_t *recording_output;

static void set_error(struct gp_output *o, const char *msg)
{
	o->failed = true;
	snprintf(o->error, sizeof(o->error), "%s", msg);
	blog(LOG_ERROR, "[" PLUGIN "] %s", msg);
}

static const char *gp_get_name(void *type_data)
{
	(void)type_data;
	return "Celeste Gameplay Recorder";
}

static void *gp_create(obs_data_t *settings, obs_output_t *output)
{
	struct gp_output *o = bzalloc(sizeof(*o));
	o->output = output;
	InitializeSRWLock(&o->alock);
	snprintf(o->dir, sizeof(o->dir), "%s", obs_data_get_string(settings, "dir"));
	snprintf(o->ffmpeg, sizeof(o->ffmpeg), "%s", obs_data_get_string(settings, "ffmpeg"));
	const char *args = obs_data_get_string(settings, "video_args");
	snprintf(o->video_args, sizeof(o->video_args), "%s",
		 (args && *args) ? args : "-c:v libx264 -preset veryfast -crf 16");
	o->audio_offset_ms = obs_data_get_double(settings, "audio_offset_ms");
	return o;
}

static void free_audio(struct gp_output *o)
{
	for (size_t i = 0; i < o->nchunks; i++)
		bfree(o->chunks[i].data);
	bfree(o->chunks);
	bfree(o->pending);
	o->chunks = NULL;
	o->pending = NULL;
	o->nchunks = o->cap = o->npending = o->pcap = 0;
}

static void gp_destroy(void *data)
{
	struct gp_output *o = data;
	free_audio(o);
	bfree(o);
}

static void write_wav_header(FILE *f, uint32_t rate, uint64_t frames)
{
	uint32_t data_bytes = (uint32_t)(frames * 2 * 4);
	uint32_t riff = 36 + data_bytes;
	uint16_t fmt_tag = 3, channels = 2, align = 8, bits = 32;
	uint32_t fmt_len = 16, byte_rate = rate * 8;
	fseek(f, 0, SEEK_SET);
	fwrite("RIFF", 1, 4, f);
	fwrite(&riff, 4, 1, f);
	fwrite("WAVEfmt ", 1, 8, f);
	fwrite(&fmt_len, 4, 1, f);
	fwrite(&fmt_tag, 2, 1, f);
	fwrite(&channels, 2, 1, f);
	fwrite(&rate, 4, 1, f);
	fwrite(&byte_rate, 4, 1, f);
	fwrite(&align, 2, 1, f);
	fwrite(&bits, 2, 1, f);
	fwrite("data", 1, 4, f);
	fwrite(&data_bytes, 4, 1, f);
}

static bool spawn(const char *cmdline, HANDLE *proc, HANDLE *stdin_wr, const char *log_path)
{
	SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
	HANDLE rd = NULL, wr = NULL, log = INVALID_HANDLE_VALUE;
	if (stdin_wr) {
		if (!CreatePipe(&rd, &wr, &sa, 64 * 1024 * 1024))
			return false;
		SetHandleInformation(wr, HANDLE_FLAG_INHERIT, 0);
	}
	log = CreateFileA(log_path, GENERIC_WRITE, FILE_SHARE_READ, &sa, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
	STARTUPINFOA si = {0};
	si.cb = sizeof(si);
	si.dwFlags = STARTF_USESTDHANDLES;
	si.hStdInput = rd ? rd : GetStdHandle(STD_INPUT_HANDLE);
	si.hStdOutput = log;
	si.hStdError = log;
	PROCESS_INFORMATION pi = {0};
	char *cmd = bstrdup(cmdline);
	BOOL ok = CreateProcessA(NULL, cmd, NULL, NULL, TRUE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi);
	bfree(cmd);
	if (rd)
		CloseHandle(rd);
	if (log != INVALID_HANDLE_VALUE)
		CloseHandle(log);
	if (!ok) {
		if (wr)
			CloseHandle(wr);
		return false;
	}
	CloseHandle(pi.hThread);
	*proc = pi.hProcess;
	if (stdin_wr)
		*stdin_wr = wr;
	return true;
}

static bool gp_start(void *data)
{
	struct gp_output *o = data;
	struct obs_video_info ovi;
	struct obs_audio_info oai;
	if (!obs_get_video_info(&ovi) || !obs_get_audio_info(&oai)) {
		set_error(o, "video/audio not initialised");
		return false;
	}
	o->width = ovi.output_width;
	o->height = ovi.output_height;
	o->fps_num = ovi.fps_num;
	o->fps_den = ovi.fps_den;
	o->rate = oai.samples_per_sec;
	o->frame_ns = (uint64_t)(1000000000.0 * ovi.fps_den / ovi.fps_num + 0.5);
	CreateDirectoryA(o->dir, NULL);

	struct video_scale_info vsi = {VIDEO_FORMAT_NV12, o->width, o->height, VIDEO_RANGE_PARTIAL, VIDEO_CS_709};
	obs_output_set_video_conversion(o->output, &vsi);
	struct audio_convert_info aci = {o->rate, AUDIO_FORMAT_FLOAT, SPEAKERS_STEREO, true};
	obs_output_set_audio_conversion(o->output, &aci);

	char path[MAX_PATH * 2], cmd[4096], log_path[MAX_PATH * 2];
	snprintf(cmd, sizeof(cmd),
		 "\"%s\" -hide_banner -loglevel warning -y -f rawvideo -pix_fmt nv12 -s %ux%u -framerate %u/%u "
		 "-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 -i - %s -pix_fmt yuv420p "
		 "-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \"%s\\video.mkv\"",
		 o->ffmpeg, o->width, o->height, o->fps_num, o->fps_den, o->video_args, o->dir);
	snprintf(log_path, sizeof(log_path), "%s\\ffmpeg_video.log", o->dir);
	if (!spawn(cmd, &o->proc, &o->stdin_wr, log_path)) {
		set_error(o, "cannot start ffmpeg (check the ffmpeg path)");
		return false;
	}
	snprintf(path, sizeof(path), "%s\\audio.wav", o->dir);
	o->wav = fopen(path, "wb");
	snprintf(path, sizeof(path), "%s\\frames.csv", o->dir);
	o->csv = fopen(path, "w");
	if (!o->wav || !o->csv) {
		set_error(o, "cannot create output files");
		return false;
	}
	write_wav_header(o->wav, o->rate, 0);
	fprintf(o->csv, "index,game_frame,chapter_ticks,obs_ts_ns\n");

	if (!obs_output_can_begin_data_capture(o->output, 0)) {
		set_error(o, "cannot begin data capture");
		return false;
	}
	active_output = o;
	obs_output_begin_data_capture(o->output, 0);
	blog(LOG_INFO, "[" PLUGIN "] recording to %s (%ux%u @ %u/%u, audio %u Hz)", o->dir, o->width, o->height,
	     o->fps_num, o->fps_den, o->rate);
	return true;
}

/* 把 [t, t + 1/fps) 的音频写进 WAV；没有数据的部分补静音 */
static void write_audio_interval(struct gp_output *o, uint64_t t)
{
	uint32_t n = (uint32_t)((uint64_t)o->rate * o->fps_den / o->fps_num);
	float *buf = bzalloc((size_t)n * 2 * sizeof(float));
	const double ns_per_sample = 1e9 / o->rate;
	for (size_t i = 0; i < o->nchunks; i++) {
		struct chunk *c = &o->chunks[i];
		double c_start = (double)c->ts;
		double c_end = c_start + c->frames * ns_per_sample;
		double t0 = (double)t;
		if (c_end <= t0 || c_start >= t0 + n * ns_per_sample)
			continue;
		long long k_from = (long long)ceil((c_start - t0) / ns_per_sample);
		long long k_to = (long long)ceil((c_end - t0) / ns_per_sample);
		if (k_from < 0)
			k_from = 0;
		if (k_to > n)
			k_to = n;
		for (long long k = k_from; k < k_to; k++) {
			long long idx = llround((t0 + k * ns_per_sample - c_start) / ns_per_sample);
			if (idx < 0 || idx >= c->frames)
				continue;
			buf[k * 2] = c->data[idx * 2];
			buf[k * 2 + 1] = c->data[idx * 2 + 1];
		}
	}
	fwrite(buf, sizeof(float), (size_t)n * 2, o->wav);
	o->wav_frames += n;
	bfree(buf);
}

static uint64_t audio_end(struct gp_output *o)
{
	if (!o->nchunks)
		return 0;
	struct chunk *c = &o->chunks[o->nchunks - 1];
	return c->ts + (uint64_t)((double)c->frames * 1e9 / o->rate);
}

/* 音频到齐的保留帧依次写出 */
static void flush_pending(struct gp_output *o, bool force)
{
	size_t done = 0;
	uint64_t end = audio_end(o);
	while (done < o->npending) {
		uint64_t t = o->pending[done];
		if (!force && t + o->frame_ns > end)
			break;
		write_audio_interval(o, t);
		done++;
	}
	if (done) {
		memmove(o->pending, o->pending + done, (o->npending - done) * sizeof(uint64_t));
		o->npending -= done;
	}
	/* 丢掉不再需要的旧音频 */
	uint64_t keep_from = o->npending ? o->pending[0] : end;
	uint64_t horizon = (uint64_t)AUDIO_RING_SECONDS * 1000000000ULL;
	if (end > horizon && keep_from > end - horizon)
		keep_from = end - horizon;
	size_t drop = 0;
	while (drop < o->nchunks) {
		struct chunk *c = &o->chunks[drop];
		uint64_t cend = c->ts + (uint64_t)((double)c->frames * 1e9 / o->rate);
		if (cend >= keep_from)
			break;
		bfree(c->data);
		drop++;
	}
	if (drop) {
		memmove(o->chunks, o->chunks + drop, (o->nchunks - drop) * sizeof(struct chunk));
		o->nchunks -= drop;
	}
}

static bool write_all(HANDLE h, const uint8_t *p, size_t n)
{
	while (n) {
		DWORD w = 0;
		if (!WriteFile(h, p, (DWORD)(n > (1u << 30) ? (1u << 30) : n), &w, NULL) || !w)
			return false;
		p += w;
		n -= w;
	}
	return true;
}

static bool write_plane(HANDLE h, const uint8_t *data, uint32_t linesize, uint32_t width, uint32_t rows)
{
	if (linesize == width)
		return write_all(h, data, (size_t)width * rows);
	for (uint32_t y = 0; y < rows; y++)
		if (!write_all(h, data + (size_t)y * linesize, width))
			return false;
	return true;
}

static void gp_raw_video(void *data, struct video_data *frame)
{
	struct gp_output *o = data;
	long long game_frame, ticks;
	if (o->failed || !keep_take(frame->timestamp, &game_frame, &ticks))
		return;
	bool ok = write_plane(o->stdin_wr, frame->data[0], frame->linesize[0], o->width, o->height) &&
		  write_plane(o->stdin_wr, frame->data[1], frame->linesize[1], o->width, o->height / 2);
	if (!ok) {
		set_error(o, "ffmpeg pipe closed");
		return;
	}
	fprintf(o->csv, "%lld,%lld,%lld,%llu\n", o->written, game_frame, ticks, (unsigned long long)frame->timestamp);
	o->written++;

	uint64_t start = frame->timestamp;
	double off = o->audio_offset_ms * 1e6;
	start = off >= 0 ? start + (uint64_t)off : start - (uint64_t)(-off);
	AcquireSRWLockExclusive(&o->alock);
	if (o->npending == o->pcap) {
		o->pcap = o->pcap ? o->pcap * 2 : 256;
		o->pending = brealloc(o->pending, o->pcap * sizeof(uint64_t));
	}
	o->pending[o->npending++] = start;
	flush_pending(o, false);
	ReleaseSRWLockExclusive(&o->alock);
}

static void gp_raw_audio(void *data, struct audio_data *frames)
{
	struct gp_output *o = data;
	if (o->failed || !frames->frames)
		return;
	AcquireSRWLockExclusive(&o->alock);
	if (o->nchunks == o->cap) {
		o->cap = o->cap ? o->cap * 2 : 256;
		o->chunks = brealloc(o->chunks, o->cap * sizeof(struct chunk));
	}
	struct chunk *c = &o->chunks[o->nchunks++];
	c->ts = frames->timestamp;
	c->frames = frames->frames;
	c->data = bmemdup(frames->data[0], (size_t)frames->frames * 2 * sizeof(float));
	flush_pending(o, false);
	ReleaseSRWLockExclusive(&o->alock);
}

static DWORD WINAPI finish_thread(LPVOID param)
{
	struct gp_output *o = param;
	char cmd[4096], log_path[MAX_PATH * 2];
	if (o->proc) {
		WaitForSingleObject(o->proc, INFINITE);
		CloseHandle(o->proc);
		o->proc = NULL;
	}
	if (!o->failed && o->written > 0) {
		snprintf(cmd, sizeof(cmd),
			 "\"%s\" -hide_banner -loglevel warning -y -i \"%s\\video.mkv\" -i \"%s\\audio.wav\" -map 0:v -map 1:a "
			 "-c:v copy -c:a aac -b:a 256k \"%s\\gameplay.mkv\"",
			 o->ffmpeg, o->dir, o->dir, o->dir);
		snprintf(log_path, sizeof(log_path), "%s\\ffmpeg_mux.log", o->dir);
		HANDLE mux;
		if (spawn(cmd, &mux, NULL, log_path)) {
			WaitForSingleObject(mux, INFINITE);
			CloseHandle(mux);
		}
	}
	send_line("S {\"recording\":false,\"finished\":true,\"frames\":%lld,\"error\":\"%s\"}", o->written,
		  o->failed ? o->error : "");
	blog(LOG_INFO, "[" PLUGIN "] finished: %lld gameplay frames", o->written);
	return 0;
}

static void gp_stop(void *data, uint64_t ts)
{
	(void)ts;
	struct gp_output *o = data;
	obs_output_end_data_capture(o->output);
	active_output = NULL;
	AcquireSRWLockExclusive(&o->alock);
	if (o->wav) {
		flush_pending(o, true);
		write_wav_header(o->wav, o->rate, o->wav_frames);
		fclose(o->wav);
		o->wav = NULL;
	}
	ReleaseSRWLockExclusive(&o->alock);
	if (o->csv) {
		fclose(o->csv);
		o->csv = NULL;
	}
	if (o->stdin_wr) {
		CloseHandle(o->stdin_wr); /* ffmpeg 读到结尾后收尾 */
		o->stdin_wr = NULL;
	}
	HANDLE t = CreateThread(NULL, 0, finish_thread, o, 0, NULL);
	if (t)
		CloseHandle(t);
}

static struct obs_output_info gp_info = {
	.id = "celeste_gameplay_output",
	.flags = OBS_OUTPUT_AV,
	.get_name = gp_get_name,
	.create = gp_create,
	.destroy = gp_destroy,
	.start = gp_start,
	.stop = gp_stop,
	.raw_video = gp_raw_video,
	.raw_audio = gp_raw_audio,
};

/* ------------------------------------------------------------------ 录制指令（UI 线程执行） */

static void start_task(void *param)
{
	obs_data_t *settings = param;
	if (recording_output) {
		obs_output_stop(recording_output);
		obs_output_release(recording_output);
		recording_output = NULL;
	}
	recording_output = obs_output_create("celeste_gameplay_output", "celeste-gameplay", settings, NULL);
	obs_data_release(settings);
	if (recording_output && obs_output_start(recording_output)) {
		InterlockedExchange(&g.lockstep, 1);
		send_line("L 1");
		send_line("S {\"recording\":true,\"frames\":0}");
	} else {
		const char *err = recording_output ? obs_output_get_last_error(recording_output) : "create failed";
		send_line("S {\"recording\":false,\"error\":\"start failed: %s\"}", err ? err : "unknown");
	}
}

static void stop_task(void *param)
{
	(void)param;
	if (recording_output) {
		obs_output_stop(recording_output);
		obs_output_release(recording_output);
		recording_output = NULL;
	}
}

static void handle_command(const char *json)
{
	obs_data_t *data = obs_data_create_from_json(json);
	if (!data)
		return;
	const char *action = obs_data_get_string(data, "action");
	if (strcmp(action, "start") == 0) {
		obs_queue_task(OBS_TASK_UI, start_task, data, false); /* data 交给任务释放 */
		return;
	}
	if (strcmp(action, "stop") == 0) {
		InterlockedExchange(&g.lockstep, 0); /* 先停锁步，桥接恢复正常节拍 */
		send_line("L 0");
		obs_queue_task(OBS_TASK_UI, stop_task, NULL, false);
	}
	obs_data_release(data);
}

/* ------------------------------------------------------------------ 网络线程 */

static void handle_line(char *line)
{
	if (line[0] == 'F' || line[0] == 'N') {
		long long id = 0, frame = 0, ticks = 0;
		if (line[0] == 'F')
			sscanf(line + 2, "%lld %lld %lld", &id, &frame, &ticks);
		else
			sscanf(line + 2, "%lld", &id);
		EnterCriticalSection(&g.lock);
		g.reply_kind = line[0];
		g.reply_id = id;
		g.reply_frame = frame;
		g.reply_ticks = ticks;
		LeaveCriticalSection(&g.lock);
		WakeAllConditionVariable(&g.reply_cv);
	} else if (line[0] == 'R' && line[1] == ' ') {
		handle_command(line + 2);
	}
}

static DWORD WINAPI net_thread(LPVOID param)
{
	(void)param;
	WSADATA wsa;
	WSAStartup(MAKEWORD(2, 2), &wsa);
	char buf[65536];
	size_t len = 0;
	ULONGLONG last_status = 0;
	while (!g.stopping) {
		SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
		struct sockaddr_in addr = {0};
		addr.sin_family = AF_INET;
		addr.sin_port = htons(CLOCK_PORT);
		addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
		if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
			closesocket(s);
			Sleep(1000);
			continue;
		}
		BOOL nodelay = TRUE;
		setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&nodelay, sizeof(nodelay));
		DWORD timeout = 500;
		setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof(timeout));
		EnterCriticalSection(&g.lock);
		g.sock = s;
		g.outstanding = 0;
		g.reply_kind = 0;
		LeaveCriticalSection(&g.lock);
		InterlockedExchange(&g.connected, 1);
		blog(LOG_INFO, "[" PLUGIN "] connected to bridge clock port %d", CLOCK_PORT);
		if (g.lockstep)
			send_line("L 1");
		len = 0;
		while (!g.stopping) {
			int n = recv(s, buf + len, (int)(sizeof(buf) - 1 - len), 0);
			if (n == 0)
				break;
			if (n < 0) {
				if (WSAGetLastError() == WSAETIMEDOUT) {
					n = 0;
				} else {
					break;
				}
			}
			len += (size_t)n;
			buf[len] = 0;
			char *start = buf, *nl;
			while ((nl = strchr(start, '\n')) != NULL) {
				*nl = 0;
				if (nl > start && nl[-1] == '\r')
					nl[-1] = 0;
				handle_line(start);
				start = nl + 1;
			}
			len = strlen(start);
			memmove(buf, start, len + 1);
			ULONGLONG now = GetTickCount64();
			if (now - last_status > 1000) {
				last_status = now;
				struct gp_output *o = active_output;
				send_line("S {\"recording\":%s,\"frames\":%lld,\"tokens\":%lld,\"kept\":%lld,\"late\":%lld,\"error\":\"%s\"}",
					  o ? "true" : "false", o ? o->written : 0LL, g.tokens, g.kept, g.late_ticks,
					  (o && o->failed) ? o->error : "");
			}
		}
		InterlockedExchange(&g.connected, 0);
		EnterCriticalSection(&g.lock);
		g.sock = INVALID_SOCKET;
		LeaveCriticalSection(&g.lock);
		WakeAllConditionVariable(&g.reply_cv);
		closesocket(s);
		blog(LOG_INFO, "[" PLUGIN "] disconnected from bridge");
	}
	WSACleanup();
	return 0;
}

/* ------------------------------------------------------------------ 模块入口 */

bool obs_module_load(void)
{
	InitializeCriticalSection(&g.lock);
	InitializeConditionVariable(&g.reply_cv);
	InitializeSRWLock(&keep.lock);
	g.sock = INVALID_SOCKET;
	obs_register_output(&gp_info);
	obs_add_tick_callback(on_tick, NULL);
	g.net_thread = CreateThread(NULL, 0, net_thread, NULL, 0, NULL);
	blog(LOG_INFO, "[" PLUGIN "] loaded");
	return true;
}

void obs_module_unload(void)
{
	InterlockedExchange(&g.stopping, 1);
	obs_remove_tick_callback(on_tick, NULL);
	if (g.net_thread) {
		WaitForSingleObject(g.net_thread, 3000);
		CloseHandle(g.net_thread);
	}
	if (recording_output) {
		obs_output_stop(recording_output);
		obs_output_release(recording_output);
		recording_output = NULL;
	}
}

const char *obs_module_description(void)
{
	return "Celeste AI lockstep recorder: records only the frames the game actually advanced";
}
