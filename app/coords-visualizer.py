import re
import tkinter as tk
from tkinter import ttk, messagebox
from matplotlib.ticker import FuncFormatter
from matplotlib.patches import Circle
import math
import threading
import uuid
import queue

try:
    import pyperclip
except ImportError:
    raise SystemExit('need pyperclip: pip install pyperclip')

try:
    import matplotlib
    matplotlib.use('TkAgg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
except ImportError:
    raise SystemExit('need matplotlib: pip install matplotlib')

try:
    from room_server import RoomClient, start_server_in_background
except ImportError as e:
    raise SystemExit(
        'room_server.py is required. Put room_server.py next to coords-visualizer.py.\n'
        f'Import error: {e}'
    )

MIN_RADIUS = 5000
GRID_STEP = 1000
CLIPBOARD_CHECK_MS = 100
ARROW_LENGTH = 500
RANGE_RADIUS = 512
DEFAULT_ROOM_SERVER = 'http://127.0.0.1:8765'

TP_PATTERN = re.compile(
    r'/execute\s+in\s+(?P<dimension>\S+)\s+run\s+tp\s+@s\s+'
    r'(?P<x>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<y>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<z>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<yaw>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<pitch>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*$',
    re.IGNORECASE,
)


class CoordinatePlotter:
    def __init__(self, root):
        self.root = root
        self.root.title('Coords Visualizer')
        self.root.geometry('430x400')
        self.root.minsize(430, 400)

        self.points = []
        self.undo_stack = []
        self.redo_stack = []
        self.show_range = False
        self.last_clipboard = None
        self.monitoring = True
        self.room_client = None
        self.room_events = queue.Queue()
        self.client_id = uuid.uuid4().hex
        self.local_room_points = set()

        # Start the room server automatically. It is harmless if another
        # process already owns the port; local-only use still works.
        try:
            self.server_thread = start_server_in_background()
        except Exception:
            self.server_thread = None

        self._build_ui()
        self._setup_plot()
        self.status_var.set('Monitoring clipboard...')
        self._check_clipboard()
        self._poll_room_events()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(5, 5, 5, 2))
        top.pack(fill=tk.X)
        ttk.Button(top, text='Undo', command=self.undo, width=8).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(top, text='Redo', command=self.redo, width=8).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(top, text='Reset', command=self.reset, width=8).pack(side=tk.LEFT, padx=(0, 5))
        self.range_button = ttk.Button(top, text='Range: Off', command=self.toggle_range, width=10)
        self.range_button.pack(side=tk.LEFT, padx=(0, 5))
        self.room_button = ttk.Button(top, text='Room', command=self.open_room_dialog, width=8)
        self.room_button.pack(side=tk.LEFT, padx=(0, 10))

        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var, font=('Consolas', 8)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.count_var = tk.StringVar(value='Points: 0')
        ttk.Label(top, textvariable=self.count_var, font=('Consolas', 8)).pack(side=tk.RIGHT)

        self.plot_frame = ttk.Frame(self.root)
        self.plot_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

    def _setup_plot(self):
        self.fig, self.ax = plt.subplots(figsize=(8, 7))
        self.ax.set_aspect('equal', adjustable='box')
        self.ax.xaxis.tick_top()
        self.ax.xaxis.set_label_position('top')
        self.ax.set_xlabel('X')
        self.ax.set_ylabel('Z')
        self.fig.subplots_adjust(left=0.10, right=0.97, bottom=0.02, top=0.92)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._redraw()

    def _check_clipboard(self):
        if not self.monitoring:
            return
        try:
            text = pyperclip.paste()
        except Exception:
            text = ''
        if text and text != self.last_clipboard:
            self.last_clipboard = text
            self._process_clipboard(text)
        self.root.after(CLIPBOARD_CHECK_MS, self._check_clipboard)

    def _process_clipboard(self, text):
        text = text.strip()
        match = TP_PATTERN.fullmatch(text)
        if not match:
            return
        dimension = match.group('dimension')
        if dimension.lower() != 'minecraft:overworld':
            self.status_var.set(f'ignore: {dimension} (not overworld)')
            return
        x = float(match.group('x'))
        z = float(match.group('z'))
        yaw = float(match.group('yaw'))
        pitch = float(match.group('pitch'))
        if pitch <= 0:
            self.status_var.set(f'ignore: pitch={pitch:.2f} (0 or below)')
            return
        point = {
            'id': uuid.uuid4().hex,
            'x': x,
            'z': z,
            'yaw': yaw,
            'pitch': pitch,
            'source': text,
        }
        self._add_point(point)

    def _add_point(self, point, send_to_room=True):
        self.points.append(point)
        self.redo_stack.clear()
        if send_to_room and self.room_client and self.room_client.connected:
            self.room_client.send_point(point)
        self._redraw()
        self.status_var.set(
            f"Added: X={point['x']:.0f}, Z={point['z']:.0f}, "
            f"Yaw={point['yaw']:.1f}°, Pitch={point['pitch']:.1f}°"
        )

    def undo(self):
        # Undo is intentionally local. Other room members keep their points.
        if not self.points:
            self.status_var.set('Undo able plots not found')
            return
        point = self.points.pop()
        self.undo_stack.append(point)
        self._redraw()
        self.status_var.set(f"Undo: X={point['x']:.0f}, Z={point['z']:.0f}")

    def redo(self):
        if not self.undo_stack:
            self.status_var.set('Redo able plots not found')
            return
        point = self.undo_stack.pop()
        self.points.append(point)
        self._redraw()
        self.status_var.set(f"Redo: X={point['x']:.0f}, Z={point['z']:.0f}")

    def toggle_range(self):
        self.show_range = not self.show_range
        self.range_button.configure(text='Range: On' if self.show_range else 'Range: Off')
        self._redraw()

    def reset(self):
        if self.room_client and self.room_client.connected:
            if messagebox.askyesno('Reset room', 'Reset all points in this room for everyone?'):
                self.room_client.reset()
            return
        if not self.points:
            self.status_var.set('No plots to reset')
            return
        self.points.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._redraw()
        self.status_var.set('Plots reset')

    @staticmethod
    def _normalize_yaw(yaw):
        return ((yaw + 180.0) % 360.0) - 180.0

    @staticmethod
    def _yaw_to_direction(yaw):
        yaw = math.radians(yaw)
        return -math.sin(yaw), math.cos(yaw)

    def _calculate_radius(self):
        radius = float(MIN_RADIUS)
        if not self.points:
            return radius
        max_distance = max(max(abs(p['x']), abs(p['z'])) for p in self.points)
        if max_distance > radius:
            radius = max_distance * 1.10
        radius = ((int(radius) + GRID_STEP - 1) // GRID_STEP) * GRID_STEP
        return max(radius, MIN_RADIUS)

    def _redraw(self):
        self.ax.clear()
        radius = self._calculate_radius()
        self.ax.set_xlim(-radius, radius)
        self.ax.set_ylim(radius, -radius)
        self.ax.set_aspect('equal', adjustable='box')
        tick_step = math.ceil(radius / (5 * GRID_STEP)) * GRID_STEP
        ticks = []
        for i in range(1, 6):
            value = i * tick_step
            ticks.extend([-value, value])
        ticks = [tick for tick in sorted(ticks) if abs(tick) <= radius]
        all_ticks = sorted(ticks + [0])
        self.ax.set_xticks(all_ticks)
        self.ax.set_yticks(all_ticks)

        def format_x(value, pos):
            return 'X0' if value == 0 else value
        def format_z(value, pos):
            return 'Z0' if value == 0 else value
        self.ax.xaxis.set_major_formatter(FuncFormatter(format_x))
        self.ax.yaxis.set_major_formatter(FuncFormatter(format_z))
        self.ax.tick_params(axis='both', labelsize=7)
        self.ax.grid(True, which='major', linewidth=0.7, alpha=0.35)
        self.ax.axhline(0, linewidth=1.0, alpha=0.65)
        self.ax.axvline(0, linewidth=1.0, alpha=0.65)
        self.ax.xaxis.tick_top()
        self.ax.xaxis.set_label_position('top')

        if self.show_range:
            for point in self.points:
                self.ax.add_patch(Circle(
                    (point['x'], point['z']), RANGE_RADIUS,
                    facecolor='C0', edgecolor='C0', alpha=0.18,
                    linewidth=1.0, zorder=2
                ))

        for index, point in enumerate(self.points, start=1):
            x, z = point['x'], point['z']
            dx, dz = self._yaw_to_direction(self._normalize_yaw(point['yaw']))
            self.ax.scatter(x, z, s=32, zorder=5)
            self.ax.arrow(x, z, dx * ARROW_LENGTH, dz * ARROW_LENGTH,
                          width=24, head_width=200, head_length=260,
                          length_includes_head=True, zorder=4)
            self.ax.annotate(str(index), (x, z), xytext=(7, 7),
                             textcoords='offset points', fontsize=8, zorder=6)
        self.count_var.set(f'Points: {len(self.points)}')
        self.canvas.draw_idle()

    def open_room_dialog(self):
        if self.room_client and self.room_client.token:
            if messagebox.askyesno('Room', 'Leave the current room?'):
                self.room_client.leave()
                self.room_client = None
                self.room_button.configure(text='Room')
                self.status_var.set('Left room')
            return

        dialog = tk.Toplevel(self.root)
        dialog.title('Join Room')
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        fields = [
            ('Server URL', DEFAULT_ROOM_SERVER),
            ('Room name', ''),
            ('Password', ''),
            ('User name', ''),
        ]
        vars_ = []
        for row, (label, value) in enumerate(fields):
            ttk.Label(frame, text=label, width=12).grid(row=row, column=0, sticky='w', pady=4)
            var = tk.StringVar(value=value)
            entry = ttk.Entry(frame, textvariable=var, width=36, show='*' if label == 'Password' else '')
            entry.grid(row=row, column=1, sticky='ew', pady=4)
            vars_.append(var)
        server_var, room_var, password_var, user_var = vars_
        ttk.Label(frame, text='Hamachi: use the host PC\'s Hamachi IP, e.g. http://25.x.x.x:8765',
                  font=('TkDefaultFont', 8)).grid(row=4, column=0, columnspan=2, sticky='w', pady=(4, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky='e')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side=tk.RIGHT, padx=(5, 0))

        def connect():
            server = server_var.get().strip().rstrip('/')
            room = room_var.get().strip()
            password = password_var.get()
            user = user_var.get().strip()
            if not server or not room or not user:
                messagebox.showerror('Room', 'Server URL, room name, and user name are required.', parent=dialog)
                return
            self.room_client = RoomClient(server, on_event=lambda event, data: self.room_events.put((event, data)))
            self.room_client.join(room, password, user, self.client_id)
            self.status_var.set('Connecting to room...')
            dialog.destroy()

        ttk.Button(buttons, text='Connect', command=connect).pack(side=tk.RIGHT)
        dialog.bind('<Return>', lambda event: connect())
        dialog.bind('<Escape>', lambda event: dialog.destroy())
        dialog.after(50, lambda: dialog.focus_force())

    def _poll_room_events(self):
        try:
            while True:
                event, data = self.room_events.get_nowait()
                if event == 'snapshot':
                    self.points = list(data or [])
                    self.undo_stack.clear()
                    self.redo_stack.clear()
                    self.local_room_points = {p.get('id') for p in self.points if p.get('owner_id') == self.client_id}
                    self._redraw()
                elif event == 'point_added':
                    if not isinstance(data, dict):
                        continue
                    point_id = data.get('id')
                    if any(p.get('id') == point_id for p in self.points):
                        continue
                    self.points.append(data)
                    if data.get('owner_id') == self.client_id:
                        self.local_room_points.add(point_id)
                    self._redraw()
                elif event == 'reset':
                    self.points.clear()
                    self.undo_stack.clear()
                    self.redo_stack.clear()
                    self._redraw()
                    self.status_var.set('Room reset')
                elif event == 'connected':
                    self.room_button.configure(text='Room: On')
                    self.status_var.set(f"Room connected: {data.get('room', '')}")
                elif event == 'error':
                    self.room_button.configure(text='Room')
                    self.status_var.set('Room error')
                    messagebox.showerror('Room connection error', str(data))
                elif event == 'disconnected':
                    self.room_button.configure(text='Room')
                    if self.room_client:
                        self.status_var.set('Room disconnected')
        except queue.Empty:
            pass
        self.root.after(100, self._poll_room_events)

    def bind_shortcuts(self):
        self.root.bind('<Control-z>', lambda event: self.undo())
        self.root.bind('<Control-y>', lambda event: self.redo())
        self.root.bind('<Control-Shift-Z>', lambda event: self.redo())
        self.root.bind('<Control-r>', lambda event: self.reset())

    def _on_close(self):
        self.monitoring = False
        if self.room_client:
            self.room_client.leave()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        app = CoordinatePlotter(root)
        app.bind_shortcuts()
        root.mainloop()
    except Exception as e:
        messagebox.showerror('Error', str(e))
        raise


if __name__ == '__main__':
    main()
