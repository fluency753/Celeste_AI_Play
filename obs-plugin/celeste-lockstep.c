/*
 * Celeste AI 锁步录制插件（OBS 32，Windows x64）
 *
 * 1. 锁步：录制期间，OBS 每个视频节拍（tick 回调，渲染这一帧之前）向桥接 Mod 的时钟端口发一个令牌，
 *    等桥接推进一帧并把它呈现到屏幕（最多等 WAIT_MS 毫秒），这一帧渲染时就采到了这个游戏帧。
 * 2. 只录游玩帧：插件自带一个原始画面输出（celeste_gameplay_output），只把"刚采到一个新游戏帧"的视频帧
 *    交给 ffmpeg 编码；每个保留的视频帧配上同一时段（加音频偏移）的 1/fps 秒音频，写进 WAV，结束时合成 MKV。
 *    思考期间、桥接空闲时的视频帧都不写，所以成片的帧数等于游玩帧数。
 *    规划回放的帧（游戏不推进、只重画画面，章节计时 -1）期间音乐一直在放：这时画一帧跟不上节拍，就把漏掉的节拍
 *    用下一帧补上（章节计时记 -2），声音一段不丢，音乐保持原速。
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
/* 声音对齐：录制（锁步）时桥接在游戏停住后的第一个节拍就暂停全部声音、推进前恢复，所以每段动作开头的声音都是
 * "完全静音 → 有声"的一条边。两个保留帧相隔超过 SEG_GAP_NS 就算新的一段，在预计位置附近找这条边，
 * 量出这一刻采集管线的延迟（OBS 的音频缓冲会在录制中途变化），取最近几次的中位数用于这一段。 */
#define SEG_GAP_NS 100000000ULL      /* 100 ms */
#define ONSET_WINDOW_NS 250000000LL  /* 在预计位置前后 250 ms 内找 */
#define ONSET_SILENCE 240            /* 边之前至少 5 ms（48 kHz 下 240 个样本）完全静音 */
#define ONSET_THRESHOLD 1e-5f
#define OFFSET_HISTORY 5
#define SEAM_TRUST_NS 20e6           /* 20 ms：这一段量到的延迟与中位数相差不超过它就直接用 */

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

static CRITICAL_SECTION send_lock;

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
	if (s != INVALID_SOCKET) {
		EnterCriticalSection(&send_lock); /* 多个线程都会发：整行一起发，避免交错 */
		send(s, buf, n, 0);
		LeaveCriticalSection(&send_lock);
	}
}

/* 录制状态（报告给桥接，主控通过 hello 读取） */
enum rec_state { REC_IDLE, REC_RECORDING, REC_FINISHING, REC_FINISHED, REC_FAILED };
static const char *const rec_state_names[] = {"idle", "recording", "finishing", "finished", "failed"};
static struct {
	SRWLOCK lock;
	enum rec_state state;
	long long frames;
	char dir[MAX_PATH];
	char error[512];
} rec;

static void rec_set(enum rec_state state, long long frames, const char *error)
{
	AcquireSRWLockExclusive(&rec.lock);
	rec.state = state;
	if (frames >= 0)
		rec.frames = frames;
	if (error)
		snprintf(rec.error, sizeof(rec.error), "%s", error);
	ReleaseSRWLockExclusive(&rec.lock);
}

/* JSON 字符串转义（只处理会破坏 JSON 的字符） */
static void json_escape(char *out, size_t cap, const char *in)
{
	size_t n = 0;
	for (; in && *in && n + 7 < cap; in++) {
		unsigned char c = (unsigned char)*in;
		if (c == '"' || c == '\\') {
			out[n++] = '\\';
			out[n++] = (char)c;
		} else if (c < 0x20) {
			n += (size_t)snprintf(out + n, cap - n, "\\u%04x", c);
		} else {
			out[n++] = (char)c;
		}
	}
	out[n] = 0;
}

static void send_status(void)
{
	char dir[MAX_PATH * 2], err[1100];
	AcquireSRWLockShared(&rec.lock);
	enum rec_state state = rec.state;
	long long frames = rec.frames;
	json_escape(dir, sizeof(dir), rec.dir);
	json_escape(err, sizeof(err), rec.error);
	ReleaseSRWLockShared(&rec.lock);
	send_line("S {\"state\":\"%s\",\"recording\":%s,\"frames\":%lld,\"tokens\":%lld,\"kept\":%lld,\"late\":%lld,"
		  "\"dir\":\"%s\",\"error\":\"%s\"}",
		  rec_state_names[state], state == REC_RECORDING ? "true" : "false", frames, g.tokens, g.kept, g.late_ticks,
		  dir, err);
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

struct pend {
	uint64_t ts;  /* 视频帧时间戳 */
	uint32_t seg; /* 所在的段 */
	long long index;
	bool fade_in; /* 声音从暂停恢复后的第一帧：开头淡入几毫秒（去掉接缝处的爆音） */
};

/* 暂停与恢复的接缝处各淡出、淡入 2 毫秒（48 kHz 下 96 个样本）：音乐内容是接上的，只去掉波形突变的爆音 */
#define SEAM_FADE_MS 2

enum seg_state { SEG_UNKNOWN, SEG_MEASURED, SEG_FALLBACK };

struct segment {
	uint64_t start_ts;   /* 这一段第一帧的时间戳 */
	long long first_index;
	enum seg_state state;
	double offset_ns;    /* 这一段用的声音偏移 */
	double measured_ns;  /* 这一段量到的延迟（没量到时为 NAN） */
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
	long long filled;    /* 回放帧跟不上节拍时补的帧数 */
	float *last_buf;     /* 最近写进 WAV 的那一帧声音（接缝处要回头淡出时用） */
	uint32_t last_cap;
	long long last_pos;  /* 它在 WAV 文件里的位置（字节） */
	long long last_index;
	bool last_valid;
	long long fade_out_index; /* 这一帧写出时结尾淡出（它之后是一段暂停）；-1 表示没有 */
	uint64_t prev_ts;    /* 上一个写出的保留帧（视频锁内） */
	long long prev_ticks;
	bool failed;
	bool stopping; /* gp_stop 之后，迟到的原始帧回调直接忽略 */
	SRWLOCK vlock;  /* 原始画面回调与 gp_stop 之间的互斥 */
	char error[512];
	/* 音频 */
	SRWLOCK alock;
	struct chunk *chunks;
	size_t nchunks, cap;
	struct pend *pending; /* 等待音频的保留帧 */
	size_t npending, pcap;
	/* 段与声音对齐 */
	struct segment *segs;
	size_t nsegs, segcap;
	uint64_t last_frame_ts;
	double history[OFFSET_HISTORY]; /* 最近几次量到的延迟（纳秒） */
	int nhistory, hpos;
	double cur_offset_ns;
	FILE *seg_csv;
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
	InitializeSRWLock(&o->vlock);
	snprintf(o->dir, sizeof(o->dir), "%s", obs_data_get_string(settings, "dir"));
	snprintf(o->ffmpeg, sizeof(o->ffmpeg), "%s", obs_data_get_string(settings, "ffmpeg"));
	const char *args = obs_data_get_string(settings, "video_args");
	snprintf(o->video_args, sizeof(o->video_args), "%s",
		 (args && *args) ? args : "-c:v libx264 -preset veryfast -crf 16");
	o->audio_offset_ms = obs_data_get_double(settings, "audio_offset_ms");
	o->cur_offset_ns = o->audio_offset_ms * 1e6;
	return o;
}

static void free_audio(struct gp_output *o)
{
	for (size_t i = 0; i < o->nchunks; i++)
		bfree(o->chunks[i].data);
	bfree(o->chunks);
	bfree(o->pending);
	bfree(o->segs);
	bfree(o->last_buf);
	o->last_buf = NULL;
	o->last_cap = 0;
	o->chunks = NULL;
	o->pending = NULL;
	o->segs = NULL;
	o->nchunks = o->cap = o->npending = o->pcap = o->nsegs = o->segcap = 0;
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

/* 开始失败时清理已经打开的东西 */
static bool start_failed(struct gp_output *o, const char *msg)
{
	set_error(o, msg);
	if (o->stdin_wr) {
		CloseHandle(o->stdin_wr);
		o->stdin_wr = NULL;
	}
	if (o->proc) {
		TerminateProcess(o->proc, 1);
		CloseHandle(o->proc);
		o->proc = NULL;
	}
	if (o->wav) {
		fclose(o->wav);
		o->wav = NULL;
	}
	if (o->csv) {
		fclose(o->csv);
		o->csv = NULL;
	}
	if (o->seg_csv) {
		fclose(o->seg_csv);
		o->seg_csv = NULL;
	}
	rec_set(REC_FAILED, 0, msg);
	return false;
}

static bool gp_start(void *data)
{
	struct gp_output *o = data;
	struct obs_video_info ovi;
	struct obs_audio_info oai;
	if (!obs_get_video_info(&ovi) || !obs_get_audio_info(&oai))
		return start_failed(o, "video/audio not initialised");
	o->width = ovi.output_width;
	o->height = ovi.output_height;
	o->fps_num = ovi.fps_num;
	o->fps_den = ovi.fps_den;
	o->rate = oai.samples_per_sec;
	o->frame_ns = (uint64_t)(1000000000.0 * ovi.fps_den / ovi.fps_num + 0.5);
	o->filled = 0;
	o->prev_ts = 0;
	o->prev_ticks = 0;
	o->last_valid = false;
	o->last_index = -1;
	o->fade_out_index = -1;
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
	if (!spawn(cmd, &o->proc, &o->stdin_wr, log_path))
		return start_failed(o, "cannot start ffmpeg (check the ffmpeg path)");
	snprintf(path, sizeof(path), "%s\\audio.wav", o->dir);
	o->wav = fopen(path, "wb");
	snprintf(path, sizeof(path), "%s\\frames.csv", o->dir);
	o->csv = fopen(path, "w");
	snprintf(path, sizeof(path), "%s\\segments.csv", o->dir);
	o->seg_csv = fopen(path, "w");
	if (!o->wav || !o->csv || !o->seg_csv)
		return start_failed(o, "cannot create output files (does the directory exist?)");
	write_wav_header(o->wav, o->rate, 0);
	fprintf(o->csv, "index,game_frame,chapter_ticks,obs_ts_ns\n");
	fprintf(o->seg_csv, "segment,first_index,start_ts_ns,measured_ms,offset_ms,method\n");

	if (!obs_output_can_begin_data_capture(o->output, 0))
		return start_failed(o, "cannot begin data capture");
	active_output = o;
	rec_set(REC_RECORDING, 0, "");
	obs_output_begin_data_capture(o->output, 0);
	blog(LOG_INFO, "[" PLUGIN "] recording to %s (%ux%u @ %u/%u, audio %u Hz)", o->dir, o->width, o->height,
	     o->fps_num, o->fps_den, o->rate);
	return true;
}

static void apply_fade(float *buf, uint32_t n, uint32_t fade, bool in)
{
	if (fade > n)
		fade = n;
	for (uint32_t k = 0; k < fade; k++) {
		float g = (float)(k + 1) / (float)(fade + 1);
		uint32_t i = in ? k : n - 1 - k;
		buf[i * 2] *= g;
		buf[i * 2 + 1] *= g;
	}
}

/* 已经写出的上一帧结尾淡出：回到它在 WAV 里的位置重写 */
static void fade_out_last(struct gp_output *o)
{
	if (!o->wav || !o->last_valid)
		return;
	uint32_t n = (uint32_t)((uint64_t)o->rate * o->fps_den / o->fps_num);
	apply_fade(o->last_buf, n, o->rate * SEAM_FADE_MS / 1000, false);
	long long cur = _ftelli64(o->wav);
	if (_fseeki64(o->wav, o->last_pos, SEEK_SET) == 0) {
		fwrite(o->last_buf, sizeof(float), (size_t)n * 2, o->wav);
		_fseeki64(o->wav, cur, SEEK_SET);
	}
	o->last_valid = false;
}

/* 把 [t, t + 1/fps) 的音频写进 WAV；没有数据的部分补静音 */
static void write_audio_interval(struct gp_output *o, uint64_t t, long long index, bool fade_in)
{
	if (!o->wav)
		return;
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
	uint32_t fade = o->rate * SEAM_FADE_MS / 1000;
	bool faded_out = false;
	if (fade_in)
		apply_fade(buf, n, fade, true);
	if (index == o->fade_out_index) {
		apply_fade(buf, n, fade, false);
		o->fade_out_index = -1;
		faded_out = true;
	}
	o->last_pos = _ftelli64(o->wav);
	fwrite(buf, sizeof(float), (size_t)n * 2, o->wav);
	o->wav_frames += n;
	if (o->last_cap < n * 2) {
		o->last_buf = brealloc(o->last_buf, (size_t)n * 2 * sizeof(float));
		o->last_cap = n * 2;
	}
	memcpy(o->last_buf, buf, (size_t)n * 2 * sizeof(float));
	o->last_index = index;
	o->last_valid = !faded_out; /* 已经淡出过的不再淡出 */
	bfree(buf);
}

static uint64_t audio_end(struct gp_output *o)
{
	if (!o->nchunks)
		return 0;
	struct chunk *c = &o->chunks[o->nchunks - 1];
	return c->ts + (uint64_t)((double)c->frames * 1e9 / o->rate);
}

/* 在 [from, to] 里找"至少 5 ms 完全静音之后第一个有声样本"。1 找到；0 整个范围的声音都到了但没有；-1 声音还没到齐 */
static int find_onset(struct gp_output *o, int64_t from, int64_t to, uint64_t *onset)
{
	const double ns_per_sample = 1e9 / o->rate;
	long long silent = 0;
	int64_t covered = from; /* 已经检查到的时间 */
	for (size_t i = 0; i < o->nchunks; i++) {
		struct chunk *c = &o->chunks[i];
		double c_start = (double)c->ts;
		for (uint32_t k = 0; k < c->frames; k++) {
			double t = c_start + k * ns_per_sample;
			if (t < (double)from)
				continue;
			if (t > (double)to)
				return 0;
			if ((int64_t)t > covered + (int64_t)(2 * ns_per_sample))
				silent = 0; /* 采集数据有空档：静音计数重新开始 */
			covered = (int64_t)t;
			float l = c->data[k * 2], r = c->data[k * 2 + 1];
			if (fabsf(l) > ONSET_THRESHOLD || fabsf(r) > ONSET_THRESHOLD) {
				if (silent >= ONSET_SILENCE) {
					*onset = (uint64_t)t;
					return 1;
				}
				silent = 0;
			} else {
				silent++;
			}
		}
	}
	return (int64_t)audio_end(o) >= to ? 0 : -1;
}

static int cmp_double(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;
	return x < y ? -1 : x > y;
}

/* 确定一段的声音偏移；声音还没到齐时返回 false（等下一次） */
static bool resolve_segment(struct gp_output *o, struct segment *sg, uint32_t seg_index, bool force)
{
	/* 桥接在这一段第一帧推进之前恢复声音，指令在第一帧的 Audio.Update 里生效：边应该对齐到第一帧的开头
	 * （之前在第一帧之后才恢复、对齐到第二帧，每段开头都有一帧静音） */
	int64_t target = (int64_t)sg->start_ts;
	int64_t center = target + (int64_t)o->cur_offset_ns;
	uint64_t onset = 0;
	int r = find_onset(o, center - ONSET_WINDOW_NS, center + ONSET_WINDOW_NS, &onset);
	if (r < 0 && !force)
		return false;
	sg->measured_ns = NAN;
	if (r == 1) {
		sg->measured_ns = (double)((int64_t)onset - target);
		o->history[o->hpos] = sg->measured_ns;
		o->hpos = (o->hpos + 1) % OFFSET_HISTORY;
		if (o->nhistory < OFFSET_HISTORY)
			o->nhistory++;
		double sorted[OFFSET_HISTORY];
		memcpy(sorted, o->history, sizeof(double) * o->nhistory);
		qsort(sorted, o->nhistory, sizeof(double), cmp_double);
		o->cur_offset_ns = sorted[o->nhistory / 2];
		sg->state = SEG_MEASURED;
	} else {
		sg->state = SEG_FALLBACK;
	}
	/* 这一段量到的值与最近几次的中位数相差不大（FMOD 混音按块生效，每段差几毫秒）时直接用它，恢复的那一刻正好落在
	 * 第一帧开头；差得多（误把别的声音当成恢复的边）时用中位数 */
	sg->offset_ns = r == 1 && fabs(sg->measured_ns - o->cur_offset_ns) <= SEAM_TRUST_NS ? sg->measured_ns : o->cur_offset_ns;
	if (o->seg_csv) {
		if (r == 1)
			fprintf(o->seg_csv, "%u,%lld,%llu,%.2f,%.2f,measured\n", seg_index, sg->first_index,
				(unsigned long long)sg->start_ts, sg->measured_ns / 1e6, sg->offset_ns / 1e6);
		else
			fprintf(o->seg_csv, "%u,%lld,%llu,,%.2f,previous\n", seg_index, sg->first_index,
				(unsigned long long)sg->start_ts, sg->offset_ns / 1e6);
	}
	return true;
}

/* 音频到齐的保留帧依次写出 */
static void flush_pending(struct gp_output *o, bool force)
{
	size_t done = 0;
	uint64_t end = audio_end(o);
	while (done < o->npending) {
		struct pend *p = &o->pending[done];
		struct segment *sg = &o->segs[p->seg];
		if (sg->state == SEG_UNKNOWN && !resolve_segment(o, sg, p->seg, force))
			break;
		int64_t t = (int64_t)p->ts + (int64_t)sg->offset_ns;
		if (t < 0)
			t = 0;
		if (!force && (uint64_t)t + o->frame_ns > end)
			break;
		write_audio_interval(o, (uint64_t)t, p->index, p->fade_in);
		done++;
	}
	if (done) {
		memmove(o->pending, o->pending + done, (o->npending - done) * sizeof(struct pend));
		o->npending -= done;
	}
	/* 丢掉不再需要的旧音频（还在等的帧往前留出找边的范围） */
	uint64_t keep_from = end;
	if (o->npending) {
		int64_t first = (int64_t)o->pending[0].ts + (int64_t)o->cur_offset_ns - 2 * ONSET_WINDOW_NS;
		keep_from = first > 0 ? (uint64_t)first : 0;
	}
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
	AcquireSRWLockExclusive(&o->vlock);
	if (o->stopping || o->failed || !keep_take(frame->timestamp, &game_frame, &ticks)) {
		ReleaseSRWLockExclusive(&o->vlock);
		return;
	}
	/* 规划回放帧（章节计时 -1）跟不上节拍时（一帧要画很多线），中间漏掉的节拍用这一帧补上：回放时音乐一直在放，
	 * 不补的话漏掉那几个节拍的声音会被跳过，录像里的音乐听起来像加速。补的帧章节计时记为 -2。游玩帧不补，
	 * 成片里的游玩帧数仍然等于游戏推进的帧数。 */
	int fill = 0;
	if (ticks == -1 && o->prev_ticks == -1 && o->written > 0 && frame->timestamp > o->prev_ts &&
	    frame->timestamp - o->prev_ts <= SEG_GAP_NS) {
		long long k = (long long)((frame->timestamp - o->prev_ts + o->frame_ns / 2) / o->frame_ns) - 1;
		if (k > 0)
			fill = (int)k;
	}
	uint64_t stamps[1 + SEG_GAP_NS / 1000000ULL];
	long long first = o->written;
	int copies = 0;
	for (int j = 0; j <= fill; j++) {
		uint64_t ts = j < fill ? o->prev_ts + (uint64_t)(j + 1) * o->frame_ns : frame->timestamp;
		if (!write_plane(o->stdin_wr, frame->data[0], frame->linesize[0], o->width, o->height) ||
		    !write_plane(o->stdin_wr, frame->data[1], frame->linesize[1], o->width, o->height / 2)) {
			set_error(o, "ffmpeg pipe closed");
			ReleaseSRWLockExclusive(&o->vlock);
			return;
		}
		fprintf(o->csv, "%lld,%lld,%lld,%llu\n", o->written, game_frame, j < fill ? -2LL : ticks,
			(unsigned long long)ts);
		o->written++;
		stamps[copies++] = ts;
	}
	o->filled += fill;
	o->prev_ts = frame->timestamp;
	o->prev_ticks = ticks;
	rec_set(REC_RECORDING, o->written, NULL);
	ReleaseSRWLockExclusive(&o->vlock);

	AcquireSRWLockExclusive(&o->alock);
	if (o->stopping) { /* 两把锁之间 gp_stop 已经收尾 */
		ReleaseSRWLockExclusive(&o->alock);
		return;
	}
	for (int j = 0; j < copies; j++) {
		uint64_t ts = stamps[j];
		/* 和上一个保留帧之间隔了一个节拍以上：声音在这中间暂停过，接缝两边各淡出、淡入一点 */
		bool seam = o->nsegs > 0 && ts - o->last_frame_ts > o->frame_ns * 3 / 2;
		if (seam) {
			if (o->last_valid && o->last_index == first + j - 1)
				fade_out_last(o);
			else
				o->fade_out_index = first + j - 1;
		}
		if (o->nsegs == 0 || ts - o->last_frame_ts > SEG_GAP_NS) {
			if (o->nsegs == o->segcap) {
				o->segcap = o->segcap ? o->segcap * 2 : 64;
				o->segs = brealloc(o->segs, o->segcap * sizeof(struct segment));
			}
			struct segment *sg = &o->segs[o->nsegs++];
			memset(sg, 0, sizeof(*sg));
			sg->start_ts = ts;
			sg->first_index = first + j;
			sg->state = SEG_UNKNOWN;
		}
		o->last_frame_ts = ts;
		if (o->npending == o->pcap) {
			o->pcap = o->pcap ? o->pcap * 2 : 256;
			o->pending = brealloc(o->pending, o->pcap * sizeof(struct pend));
		}
		o->pending[o->npending].ts = ts;
		o->pending[o->npending].seg = (uint32_t)(o->nsegs - 1);
		o->pending[o->npending].index = first + j;
		o->pending[o->npending].fade_in = seam;
		o->npending++;
	}
	flush_pending(o, false);
	ReleaseSRWLockExclusive(&o->alock);
}

static void gp_raw_audio(void *data, struct audio_data *frames)
{
	struct gp_output *o = data;
	if (o->failed || !frames->frames)
		return;
	AcquireSRWLockExclusive(&o->alock);
	if (o->stopping) {
		ReleaseSRWLockExclusive(&o->alock);
		return;
	}
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

/* 收尾任务：输出对象可能在 ffmpeg 收尾前就被释放，所以把需要的东西单独拷一份 */
struct finish_job {
	HANDLE proc;
	char ffmpeg[MAX_PATH];
	char dir[MAX_PATH];
	long long written;
	bool failed;
	char error[512];
};

static DWORD WINAPI finish_thread(LPVOID param)
{
	struct finish_job *job = param;
	char cmd[4096], log_path[MAX_PATH * 2];
	if (job->proc) {
		WaitForSingleObject(job->proc, INFINITE);
		DWORD code = 0;
		GetExitCodeProcess(job->proc, &code);
		CloseHandle(job->proc);
		if (code != 0 && !job->failed) {
			job->failed = true;
			snprintf(job->error, sizeof(job->error), "ffmpeg (video) exited with code %lu, see ffmpeg_video.log",
				 (unsigned long)code);
		}
	}
	if (!job->failed && job->written > 0) {
		snprintf(cmd, sizeof(cmd),
			 "\"%s\" -hide_banner -loglevel warning -y -i \"%s\\video.mkv\" -i \"%s\\audio.wav\" -map 0:v -map 1:a "
			 "-c:v copy -c:a aac -b:a 256k \"%s\\gameplay.mkv\"",
			 job->ffmpeg, job->dir, job->dir, job->dir);
		snprintf(log_path, sizeof(log_path), "%s\\ffmpeg_mux.log", job->dir);
		HANDLE mux;
		if (spawn(cmd, &mux, NULL, log_path)) {
			WaitForSingleObject(mux, INFINITE);
			DWORD code = 0;
			GetExitCodeProcess(mux, &code);
			CloseHandle(mux);
			if (code != 0) {
				job->failed = true;
				snprintf(job->error, sizeof(job->error), "ffmpeg (mux) exited with code %lu, see ffmpeg_mux.log",
					 (unsigned long)code);
			}
		} else {
			job->failed = true;
			snprintf(job->error, sizeof(job->error), "cannot start ffmpeg for muxing");
		}
	}
	rec_set(job->failed ? REC_FAILED : REC_FINISHED, job->written, job->failed ? job->error : "");
	send_status();
	blog(LOG_INFO, "[" PLUGIN "] finished: %lld gameplay frames%s%s", job->written, job->failed ? ", error: " : "",
	     job->failed ? job->error : "");
	bfree(job);
	return 0;
}

static void gp_stop(void *data, uint64_t ts)
{
	(void)ts;
	struct gp_output *o = data;
	if (active_output != o)
		return; /* 没有成功开始，或已经停过 */
	active_output = NULL;
	obs_output_end_data_capture(o->output);
	/* 迟到的原始帧回调可能还在路上：先拿到两把锁再关文件 */
	AcquireSRWLockExclusive(&o->vlock);
	AcquireSRWLockExclusive(&o->alock);
	o->stopping = true;
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
	if (o->seg_csv) {
		fclose(o->seg_csv);
		o->seg_csv = NULL;
	}
	if (o->stdin_wr) {
		CloseHandle(o->stdin_wr); /* ffmpeg 读到结尾后收尾 */
		o->stdin_wr = NULL;
	}
	struct finish_job *job = bzalloc(sizeof(*job));
	job->proc = o->proc;
	o->proc = NULL;
	snprintf(job->ffmpeg, sizeof(job->ffmpeg), "%s", o->ffmpeg);
	snprintf(job->dir, sizeof(job->dir), "%s", o->dir);
	job->written = o->written;
	job->failed = o->failed;
	snprintf(job->error, sizeof(job->error), "%s", o->error);
	ReleaseSRWLockExclusive(&o->vlock);
	rec_set(REC_FINISHING, job->written, NULL);
	HANDLE t = CreateThread(NULL, 0, finish_thread, job, 0, NULL);
	if (t)
		CloseHandle(t);
	else
		finish_thread(job);
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

static void stop_current(void)
{
	if (!recording_output)
		return;
	/* 锁步已停：等已经采到、还在输出队列里的游戏帧交给录制输出（最多 1 秒），再停止 */
	ULONGLONG deadline = GetTickCount64() + 1000;
	for (;;) {
		AcquireSRWLockShared(&keep.lock);
		unsigned left = keep.count;
		ReleaseSRWLockShared(&keep.lock);
		if (!left || GetTickCount64() >= deadline)
			break;
		Sleep(5);
	}
	obs_output_stop(recording_output);
	obs_output_release(recording_output);
	recording_output = NULL;
}

static void start_task(void *param)
{
	obs_data_t *settings = param;
	stop_current();
	AcquireSRWLockExclusive(&keep.lock);
	keep.head = keep.count = 0;
	ReleaseSRWLockExclusive(&keep.lock);
	EnterCriticalSection(&g.lock);
	g.tokens = g.kept = g.late_ticks = 0;
	g.outstanding = 0;
	g.reply_kind = 0;
	LeaveCriticalSection(&g.lock);
	AcquireSRWLockExclusive(&rec.lock);
	snprintf(rec.dir, sizeof(rec.dir), "%s", obs_data_get_string(settings, "dir"));
	rec.state = REC_IDLE;
	rec.frames = 0;
	rec.error[0] = 0;
	ReleaseSRWLockExclusive(&rec.lock);

	recording_output = obs_output_create("celeste_gameplay_output", "celeste-gameplay", settings, NULL);
	obs_data_release(settings);
	if (recording_output && obs_output_start(recording_output)) {
		InterlockedExchange(&g.lockstep, 1);
		send_line("L 1");
	} else {
		char msg[600];
		const char *err = recording_output ? obs_output_get_last_error(recording_output) : NULL;
		AcquireSRWLockShared(&rec.lock);
		bool have = rec.error[0] != 0;
		ReleaseSRWLockShared(&rec.lock);
		if (!have) {
			snprintf(msg, sizeof(msg), "start failed: %s", err ? err : "cannot create output");
			rec_set(REC_FAILED, 0, msg);
		} else {
			rec_set(REC_FAILED, 0, NULL);
		}
		if (recording_output) {
			obs_output_release(recording_output);
			recording_output = NULL;
		}
	}
	send_status();
}

static void stop_task(void *param)
{
	(void)param;
	stop_current();
	send_status();
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
				send_status();
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
	InitializeCriticalSection(&send_lock);
	InitializeConditionVariable(&g.reply_cv);
	InitializeSRWLock(&keep.lock);
	InitializeSRWLock(&rec.lock);
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
