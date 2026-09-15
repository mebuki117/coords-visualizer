import re
import tkinter as tk
from tkinter import ttk, messagebox
from matplotlib.ticker import FuncFormatter
from matplotlib.patches import Circle
import math

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


# ============================================================
# Settings
# ============================================================

MIN_RADIUS = 5000
GRID_STEP = 1000
CLIPBOARD_CHECK_MS = 100
ARROW_LENGTH = 500
RANGE_RADIUS = 512

TP_PATTERN = re.compile(
    r'/execute\s+in\s+(?P<dimension>\S+)\s+run\s+tp\s+@s\s+'
    r'(?P<x>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<y>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<z>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<yaw>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+'
    r'(?P<pitch>[+-]?(?:\d+(?:\.\d*)?|\.\d+))'
    r'\s*$',
    re.IGNORECASE,
)


# ============================================================
# Application
# ============================================================

class CoordinatePlotter:
    def __init__(self, root):
        self.root = root
        self.root.title('Coords Visualizer')
        self.root.geometry('400x400')
        self.root.minsize(400, 400)

        # Plot history.
        self.points = []
        self.undo_stack = []
        self.redo_stack = []
        self.show_range = False

        self.last_clipboard = None
        self.monitoring = True

        self._build_ui()
        self._setup_plot()

        self.status_var.set('Monitoring clipboard...')
        self._check_clipboard()

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(5, 5, 5, 2))
        top.pack(fill=tk.X)

        ttk.Button(
            top,
            text='Undo',
            command=self.undo,
            width=8
        ).pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(
            top,
            text='Redo',
            command=self.redo,
            width=8
        ).pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(
            top,
            text='Reset',
            command=self.reset,
            width=8
        ).pack(side=tk.LEFT, padx=(0, 5))

        self.range_button = ttk.Button(
            top,
            text='Range: Off',
            command=self.toggle_range,
            width=10
        )
        self.range_button.pack(side=tk.LEFT, padx=(0, 15))

        self.status_var = tk.StringVar()
        ttk.Label(
            top,
            textvariable=self.status_var,
            font=('Consolas', 8)
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.count_var = tk.StringVar(value='Points: 0')
        ttk.Label(
            top,
            textvariable=self.count_var,
            font=('Consolas', 8)
        ).pack(side=tk.RIGHT)

        # Plot area
        self.plot_frame = ttk.Frame(self.root)
        self.plot_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

    # --------------------------------------------------------
    # Matplotlib
    # --------------------------------------------------------

    def _setup_plot(self):
        self.fig, self.ax = plt.subplots(figsize=(8, 7))

        self.ax.set_aspect('equal', adjustable='box')

        # X-axis at the top
        self.ax.xaxis.tick_top()
        self.ax.xaxis.set_label_position('top')

        self.ax.set_xlabel('X')
        self.ax.set_ylabel('Z')

        # Reduce unused space at the bottom
        self.fig.subplots_adjust(
            left=0.10,
            right=0.97,
            bottom=0.02,
            top=0.92
        )

        self.canvas = FigureCanvasTkAgg(
            self.fig,
            master=self.plot_frame
        )
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self._redraw()
    
    # --------------------------------------------------------
    # Clipboard
    # --------------------------------------------------------

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
        # Clipboard may contain surrounding whitespace/newlines.
        text = text.strip()

        match = TP_PATTERN.fullmatch(text)

        if not match:
            return

        dimension = match.group('dimension')

        # Only overworld is accepted.
        if dimension.lower() != 'minecraft:overworld':
            self.status_var.set(
                f'ignore: {dimension} (not overworld)'
            )
            return

        x = float(match.group('x'))
        z = float(match.group('z'))
        yaw = float(match.group('yaw'))
        pitch = float(match.group('pitch'))

        # Pitch condition: greater than 0.
        if pitch <= 0:
            self.status_var.set(
                f'ignore: pitch={pitch:.2f} (0 or below)'
            )
            return

        point = {
            'x': x,
            'z': z,
            'yaw': yaw,
            'pitch': pitch,
            'source': text,
        }

        self._add_point(point)

    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    def _add_point(self, point):
        self.points.append(point)

        # A new operation invalidates the redo history.
        self.redo_stack.clear()

        self._redraw()

        self.status_var.set(
            f'Added: X={point['x']:.0f}, Z={point['z']:.0f}, '
            f'Yaw={point['yaw']:.1f}°, Pitch={point['pitch']:.1f}°'
        )

    def undo(self):
        if not self.points:
            self.status_var.set('Undo able plots not found')
            return

        point = self.points.pop()
        self.undo_stack.append(point)

        self._redraw()

        self.status_var.set(
            f'Undo: X={point['x']:.0f}, Z={point['z']:.0f}'
        )

    def redo(self):
        if not self.undo_stack:
            self.status_var.set('Redo able plots not found')
            return

        point = self.undo_stack.pop()
        self.points.append(point)

        self._redraw()

        self.status_var.set(
            f'Redo: X={point['x']:.0f}, Z={point['z']:.0f}'
        )

    def toggle_range(self):
        self.show_range = not self.show_range

        self.range_button.configure(
            text='Range: On' if self.show_range else 'Range: Off'
        )

        self._redraw()

    def reset(self):
        if not self.points:
            self.status_var.set('No plots to reset')
            return

        self.points.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()

        self._redraw()

        self.status_var.set('Plots reset')

    # --------------------------------------------------------
    # Plot
    # --------------------------------------------------------

    @staticmethod
    def _normalize_yaw(yaw):
        # Convert arbitrary yaw into [-180, 180).
        return ((yaw + 180.0) % 360.0) - 180.0

    @staticmethod
    def _yaw_to_direction(yaw):
        '''
        Minecraft yaw:
            0   -> Z+
            90  -> X-
            180 -> Z-
            -90 -> X+

        Matplotlib coordinates are X horizontal and Z vertical.

        dx = -sin(yaw)
        dz =  cos(yaw)
        '''
        yaw = math.radians(yaw)

        dx = -math.sin(yaw)
        dz = math.cos(yaw)

        return dx, dz

    def _calculate_radius(self):
        radius = float(MIN_RADIUS)

        if not self.points:
            return radius

        max_distance = 0.0

        for p in self.points:
            distance = max(abs(p['x']), abs(p['z']))
            max_distance = max(max_distance, distance)

        # Add a small margin, while keeping grid-friendly scaling.
        if max_distance > radius:
            radius = max_distance * 1.10

        # Round upward to a 1000-block grid.
        radius = ((int(radius) + GRID_STEP - 1) // GRID_STEP) * GRID_STEP

        return max(radius, MIN_RADIUS)

    def _redraw(self):
        self.ax.clear()

        radius = self._calculate_radius()

        # Coordinate range.
        self.ax.set_xlim(-radius, radius)
        self.ax.set_ylim(radius, -radius)

        self.ax.set_aspect('equal', adjustable='box')

        # Show 10 ticks excluding 0 (5 on each side)
        tick_step = math.ceil(radius / (5 * GRID_STEP)) * GRID_STEP

        ticks = []

        for i in range(1, 6):
            value = i * tick_step
            ticks.extend([-value, value])

        ticks.sort()

        # Keep ticks within the displayed range
        ticks = [tick for tick in ticks if abs(tick) <= radius]

        # Add 0 separately
        all_ticks = sorted(ticks + [0])

        self.ax.set_xticks(all_ticks)
        self.ax.set_yticks(all_ticks)

        def format_x(value, pos):
            if value == 0:
                return 'X0'
            return value

        def format_z(value, pos):
            if value == 0:
                return 'Z0'
            return value

        self.ax.xaxis.set_major_formatter(FuncFormatter(format_x))
        self.ax.yaxis.set_major_formatter(FuncFormatter(format_z))


        self.ax.tick_params(axis='both', labelsize=7)

        self.ax.grid(
            True,
            which='major',
            linewidth=0.7,
            alpha=0.35
        )

        # Center axes.
        self.ax.axhline(
            0,
            linewidth=1.0,
            alpha=0.65
        )
        self.ax.axvline(
            0,
            linewidth=1.0,
            alpha=0.65
        )

        self.ax.xaxis.tick_top()
        self.ax.xaxis.set_label_position('top')
        # self.ax.set_xlabel('X')
        # self.ax.set_ylabel('Z')

        # Draw a 512-block radius around every plotted point.
        if self.show_range:
            for point in self.points:
                self.ax.add_patch(
                    Circle(
                        (point['x'], point['z']),
                        RANGE_RADIUS,
                        facecolor='C0',
                        edgecolor='C0',
                        alpha=0.18,
                        linewidth=1.0,
                        zorder=2
                    )
                )

        # Draw every point.
        for index, point in enumerate(self.points, start=1):
            x = point['x']
            z = point['z']
            yaw = self._normalize_yaw(point['yaw'])

            dx, dz = self._yaw_to_direction(yaw)

            # Point.
            self.ax.scatter(
                x,
                z,
                s=32,
                zorder=5
            )

            # Direction arrow.
            self.ax.arrow(
                x,
                z,
                dx * ARROW_LENGTH,
                dz * ARROW_LENGTH,
                width=24,
                head_width=200,
                head_length=260,
                length_includes_head=True,
                zorder=4
            )

            # Point number.
            self.ax.annotate(
                str(index),
                (x, z),
                xytext=(7, 7),
                textcoords='offset points',
                fontsize=8,
                zorder=6
            )

        self.count_var.set(f'Points: {len(self.points)}')

        self.canvas.draw_idle()

    # --------------------------------------------------------
    # Keyboard shortcuts
    # --------------------------------------------------------

    def bind_shortcuts(self):
        self.root.bind('<Control-z>', lambda event: self.undo())
        self.root.bind('<Control-y>', lambda event: self.redo())
        self.root.bind('<Control-Shift-Z>', lambda event: self.redo())
        self.root.bind('<Control-r>', lambda event: self.reset())


def main():
    root = tk.Tk()

    try:
        app = CoordinatePlotter(root)
        app.bind_shortcuts()
        root.mainloop()
    except Exception as e:
        messagebox.showerror(
            'Error',
            str(e)
        )
        raise


if __name__ == '__main__':
    main()
