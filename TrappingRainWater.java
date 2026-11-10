import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Arrays;

/**
 * LeetCode 42 - 接雨水 (Trapping Rain Water)
 * 
 * 题目：给定 n 个非负整数表示柱子高度，计算下雨后能接多少雨水。
 * 示例：height = [0,1,0,2,1,0,1,3,2,1,2,1] → 输出 6
 *
 * 本文件提供三种解法，涵盖面试中常见的所有考察角度。
 */
public class TrappingRainWater {

    // ===================== 解法一：双指针（最优） =====================
    /**
     * 【推荐】双指针法
     * 
     * 核心直觉：
     * 对于位置 i，雨水量 = min(leftMax, rightMax) - height[i]
     * 
     * 双指针技巧：left 从左往右，right 从右往左。
     * 当 height[left] < height[right] 时，必有 leftMax < rightMax，
     * 因此 left 处的水量完全由 leftMax 决定，可以放心结算 left 侧。
     * 反之亦然。
     * 
     * 时间复杂度：O(n) — 每个元素访问一次
     * 空间复杂度：O(1) — 只用了几个变量
     */
    public int trap_TwoPointers(int[] height) {
        int left = 0, right = height.length - 1;
        int leftMax = 0, rightMax = 0;
        int total = 0;

        while (left < right) {
            // 哪边矮处理哪边——矮的那侧有更高的对岸兜底
            if (height[left] < height[right]) {
                if (height[left] >= leftMax) {
                    leftMax = height[left];  // 更新左边界，无法积水
                } else {
                    total += leftMax - height[left];  // 形成凹槽，积水
                }
                left++;
            } else {
                if (height[right] >= rightMax) {
                    rightMax = height[right];  // 更新右边界，无法积水
                } else {
                    total += rightMax - height[right];  // 形成凹槽，积水
                }
                right--;
            }
        }
        return total;
    }

    // ===================== 解法二：动态规划（最直观） =====================
    /**
     * 动态规划法 — 预处理左右最大高度
     * 
     * 思路：
     * 1. leftMax[i]  = max(height[0..i])    从左往右扫一遍
     * 2. rightMax[i] = max(height[i..n-1])  从右往左扫一遍
     * 3. water[i]    = min(leftMax[i], rightMax[i]) - height[i]
     * 
     * 时间复杂度：O(n) — 三次线性遍历
     * 空间复杂度：O(n) — 两个辅助数组
     */
    public int trap_DP(int[] height) {
        int n = height.length;
        if (n == 0) return 0;

        // leftMax[i]: 位置 i 左侧（含自身）的最高柱子
        int[] leftMax = new int[n];
        leftMax[0] = height[0];
        for (int i = 1; i < n; i++) {
            leftMax[i] = Math.max(leftMax[i - 1], height[i]);
        }

        // rightMax[i]: 位置 i 右侧（含自身）的最高柱子
        int[] rightMax = new int[n];
        rightMax[n - 1] = height[n - 1];
        for (int i = n - 2; i >= 0; i--) {
            rightMax[i] = Math.max(rightMax[i + 1], height[i]);
        }

        // 累加每个位置的雨水量
        int total = 0;
        for (int i = 0; i < n; i++) {
            total += Math.min(leftMax[i], rightMax[i]) - height[i];
        }
        return total;
    }

    // ===================== 解法三：单调递减栈 =====================
    /**
     * 单调递减栈法 — 按"层"计算水量
     * 
     * 思路：
     * 维护一个从栈底到栈顶严格递减的下标栈。
     * 当遇到一个比栈顶高的柱子时，说明形成了一个凹槽：
     *   - 弹出栈顶作为"槽底" bottom
     *   - 新的栈顶就是左边界，当前柱子是右边界
     *   - 宽度 = i - stack.peek() - 1
     *   - 深度 = min(height[左边界], height[右边界]) - height[槽底]
     *   - 水量 += 宽度 × 深度
     * 
     * 时间复杂度：O(n) — 每个元素入栈出栈各一次
     * 空间复杂度：O(n) — 栈空间
     */
    public int trap_MonotonicStack(int[] height) {
        Deque<Integer> stack = new ArrayDeque<>();
        int total = 0;

        for (int i = 0; i < height.length; i++) {
            // 当前柱子比栈顶高 → 形成凹槽，持续弹出结算
            while (!stack.isEmpty() && height[i] > height[stack.peek()]) {
                int bottom = stack.pop();  // 凹槽底部

                if (stack.isEmpty()) {
                    break;  // 左侧没有边界，无法积水
                }

                int left = stack.peek();           // 左边界
                int width = i - left - 1;          // 水面宽度
                int depth = Math.min(height[left], height[i]) - height[bottom];  // 水深

                total += width * depth;
            }
            stack.push(i);
        }
        return total;
    }

    // ===================== 测试 =====================
    public static void main(String[] args) {
        TrappingRainWater solution = new TrappingRainWater();

        // 测试用例 {输入, 期望输出, 描述}
        Object[][] testCases = {
            { new int[]{0, 1, 0, 2, 1, 0, 1, 3, 2, 1, 2, 1}, 6,  "经典示例" },
            { new int[]{4, 2, 0, 3, 2, 5},                  9,  "示例2" },
            { new int[]{1, 2, 3, 4, 5},                     0,  "严格递增，无积水" },
            { new int[]{5, 4, 3, 2, 1},                     0,  "严格递减，无积水" },
            { new int[]{3, 0, 2, 0, 4},                     9,  "多个凹槽" },
            { new int[]{0, 0, 0, 0},                        0,  "全零" },
            { new int[]{5},                                 0,  "单元素" },
            { new int[]{},                                  0,  "空数组" },
            { new int[]{2, 1, 0, 2},                        3,  "V字形" },
        };

        System.out.println("========== LeetCode 42 - 接雨水 ==========\n");

        boolean allPass = true;
        for (Object[] tc : testCases) {
            int[] height = (int[]) tc[0];
            int expected = (int) tc[1];
            String desc = (String) tc[2];

            int result1 = solution.trap_TwoPointers(height);
            int result2 = solution.trap_DP(height);
            int result3 = solution.trap_MonotonicStack(height);

            boolean pass = (result1 == expected && result2 == expected && result3 == expected);
            allPass &= pass;

            System.out.printf("[%s] %s → 期望=%d | 双指针=%d  DP=%d  单调栈=%d %s%n",
                pass ? "PASS" : "FAIL",
                desc,
                expected,
                result1,
                result2,
                result3,
                pass ? "" : " ❌");
        }

        System.out.println(allPass ? "\n✅ 全部通过！" : "\n❌ 存在失败用例");
    }
}
